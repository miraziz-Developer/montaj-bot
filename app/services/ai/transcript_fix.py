"""Transcript correction: the STT's word TIMES with Gemini's word TEXT.

On real conversational Uzbek, Gemini transcribes clearly better than timestamped STT engines (open benchmark
github.com/avazibra/uzbek-stt-bench, 2026-09: Gemini 3 Flash 15.3% WER vs 21-28% for the others), but it does
not give reliable per-word times - and captions and word-safe cuts need exactly those. So Azure/Groq keep
the timing, Gemini listens to the same audio and rewrites the text, and `align_corrected` maps the new words
onto the old times.

Never worse than the input: a window whose corrected text differs too much from the draft (a hallucination,
a summary, a wrong language) keeps the original, and so does any single run of dropped or added words longer
than a couple of words. Every failure (no audio, API error) keeps the original transcript.
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

from app.services.ai.llm import UsageInfo
from app.services.media.audio import chunk_ranges
from app.services.media.ffmpeg import run_ffmpeg
from app.services.stt.base import Transcript, TranscriptSegment, Word

logger = logging.getLogger(__name__)

WINDOW_SEC = 600  # one Gemini call per window (same size as the STT chunks)
# share of draft words the correction must keep, or the window is rejected. A real car-sale video (heavy
# Uzbek STT errors) matched 0.69 with a correct correction; unrelated text matches ~0. The per-run guards
# below catch the rest.
MIN_MATCH_RATIO = 0.5
MAX_DROPPED_RUN = 2  # longer runs of draft words missing from the correction are kept (Gemini skipped them)
MAX_ADDED_RUN = 2  # longer runs of new words are ignored (hallucination risk)
MAX_REPLACE_RATIO = 3.0  # a replaced run may change its word count at most this much
MIN_WORD_SEC = 0.02
AUDIO_TIMEOUT_SEC = 300
_APOSTROPHES = re.compile(r"['‘’ʻʼ`´]")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


class TranscriptCorrector(Protocol):
    async def correct_transcript(self, *, audio_path: Path, draft: str) -> tuple[str, UsageInfo]: ...


def _norm(token: str) -> str:
    """Comparison key: case, punctuation and the many Uzbek apostrophe variants do not matter."""
    return _NON_WORD.sub("", _APOSTROPHES.sub("", token.lower()))


@dataclass(slots=True)
class AlignedWord:
    text: str
    start: float
    end: float
    segment: int  # index of the transcript segment the word belongs to


def _spread(texts: Sequence[str], start: float, end: float, segment: int) -> list[AlignedWord]:
    """New words over [start, end], each getting time in proportion to its length."""
    weights = [max(len(_norm(t)), 1) for t in texts]
    total, span, t = sum(weights), max(end - start, MIN_WORD_SEC * len(texts)), start
    out = []
    for text, weight in zip(texts, weights, strict=True):
        step = span * weight / total
        out.append(AlignedWord(text, t, t + step, segment))
        t += step
    return out


def align_corrected(
    words: Sequence[Word], segment_of: Sequence[int], corrected: str
) -> list[AlignedWord] | None:
    """Corrected tokens mapped onto the draft words' times; None when the correction is not trustworthy."""
    new = [t for t in corrected.split() if _norm(t)]
    old_keys, new_keys = [_norm(w.text) for w in words], [_norm(t) for t in new]
    if not words or not new:
        return None
    matcher = SequenceMatcher(None, old_keys, new_keys, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    if matched / len(words) < MIN_MATCH_RATIO:
        return None

    def original(indexes: Sequence[int]) -> list[AlignedWord]:
        return [AlignedWord(words[i].text, words[i].start, words[i].end, segment_of[i]) for i in indexes]

    out: list[AlignedWord] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        old, fresh = list(range(i1, i2)), new[j1:j2]
        if op == "equal":
            out += [
                AlignedWord(fresh[k], words[i].start, words[i].end, segment_of[i]) for k, i in enumerate(old)
            ]
        elif op == "replace":
            if max(len(old), len(fresh)) / min(len(old), len(fresh)) > MAX_REPLACE_RATIO:
                out += original(old)
            else:
                out += _spread(fresh, words[i1].start, words[i2 - 1].end, segment_of[i1])
        elif op == "delete":
            if len(old) > MAX_DROPPED_RUN:  # Gemini probably skipped speech: keep what the STT heard
                out += original(old)
        elif op == "insert" and len(fresh) <= MAX_ADDED_RUN:
            if out:  # the new word(s) share the time of the word before them
                prev = out.pop()
                out += _spread([prev.text, *fresh], prev.start, prev.end, prev.segment)
            elif i1 < len(words):  # ... or, at the very start, the first moments of the word after them
                nxt = words[i1]
                out += _spread(fresh, nxt.start, nxt.start, segment_of[i1])
    return out


def _rebuild(transcript: Transcript, words: list[AlignedWord]) -> Transcript:
    segments = []
    for index, segment in enumerate(transcript.segments):
        if not segment.words:  # no word timing: nothing was aligned, keep as is
            segments.append(segment)
            continue
        mine = [w for w in words if w.segment == index]
        if not mine:
            continue
        segments.append(
            TranscriptSegment(
                start=min(segment.start, mine[0].start),
                end=max(segment.end, mine[-1].end),
                text=" ".join(w.text for w in mine),
                words=[
                    Word(text=w.text, start=w.start, end=max(w.end, w.start + MIN_WORD_SEC)) for w in mine
                ],
            )
        )
    return Transcript(language=transcript.language, segments=segments, corrected=True)


async def _window_audio(proxy: Path, start: float, end: float, out: Path) -> Path:
    await run_ffmpeg(
        ["-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(proxy),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k", str(out)],
        timeout=AUDIO_TIMEOUT_SEC,
    )  # fmt: skip
    return out


async def correct_transcript(
    llm: TranscriptCorrector,
    transcript: Transcript,
    *,
    proxy_path: Path,
    duration_sec: float,
    workdir: Path,
    job_id: object = "-",
    max_concurrency: int = 2,
) -> tuple[Transcript, UsageInfo]:
    """The transcript with Gemini-corrected words on the STT's timing (see module docstring). Never raises."""
    all_words = transcript.all_words()
    if transcript.corrected or not all_words:
        return transcript, UsageInfo()
    segment_of = [i for i, s in enumerate(transcript.segments) for _ in s.words]
    windows = chunk_ranges(duration_sec, WINDOW_SEC) or [(0.0, max(duration_sec, all_words[-1].end))]
    semaphore = asyncio.Semaphore(max(1, max_concurrency))
    workdir.mkdir(parents=True, exist_ok=True)

    async def fix(index: int, start: float, end: float) -> tuple[list[AlignedWord], UsageInfo, bool]:
        last = index == len(windows) - 1
        picked = [i for i, w in enumerate(all_words) if start <= w.start and (w.start < end or last)]
        if index == 0:  # words the STT placed a hair before 0
            picked = [i for i, w in enumerate(all_words) if w.start < end or last]
        words = [all_words[i] for i in picked]
        segments = [segment_of[i] for i in picked]
        keep = [AlignedWord(w.text, w.start, w.end, s) for w, s in zip(words, segments, strict=True)]
        if not words:
            return keep, UsageInfo(), False
        audio = workdir / f"fix_{index:03d}.ogg"
        async with semaphore:
            try:
                await _window_audio(proxy_path, start, end, audio)
                text, usage = await llm.correct_transcript(
                    audio_path=audio, draft=" ".join(w.text for w in words)
                )
            except Exception:
                logger.warning(
                    "transcript correction failed job_id=%s window=%d", job_id, index, exc_info=True
                )
                return keep, UsageInfo(), False
            finally:
                audio.unlink(missing_ok=True)
        aligned = align_corrected(words, segments, text)
        if aligned is None:
            logger.warning("transcript correction rejected job_id=%s window=%d: too different", job_id, index)
            return keep, usage, False
        return aligned, usage, True

    results = await asyncio.gather(*(fix(i, a, b) for i, (a, b) in enumerate(windows)))
    words = [w for part, _, _ in results for w in part]
    usage = sum((u for _, u, _ in results), UsageInfo())
    accepted = sum(ok for _, _, ok in results)
    logger.info(
        "transcript correction job_id=%s windows=%d/%d words=%d->%d",
        job_id, accepted, len(windows), len(all_words), len(words),
    )  # fmt: skip
    if not accepted:
        return transcript, usage
    return _rebuild(transcript, words), usage
