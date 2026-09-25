"""Token-light shapes sent to the LLM (RUNTIME_PROMPTS.md B.2 / C.2) and the planning context."""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.schemas.analysis import VideoAnalysis
from app.services.ai.presets import PresetRules, preset_rules_dict
from app.services.media.scenes import Scene
from app.services.media.silence import Silence
from app.services.stt.base import Transcript


@dataclass(frozen=True, slots=True)
class SourceInfo:
    duration_sec: float
    width: int
    height: int
    has_audio: bool
    has_speech: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_sec": round(self.duration_sec, 2),
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "has_speech": self.has_speech,
        }


@dataclass(frozen=True, slots=True)
class BrollSourceInfo:
    """One extra B-roll video (P13): muted cutaway footage, no STT pass - just per-scene visuals."""

    source_id: str
    duration_sec: float
    width: int
    height: int
    scenes: list[dict[str, Any]] = field(default_factory=list)  # [{"scene_id","start","end","description"}]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "duration_sec": round(self.duration_sec, 2),
            "width": self.width,
            "height": self.height,
            "scenes": self.scenes,
        }


def _r(value: float) -> float:
    return round(value, 2)


def scene_dicts(scenes: Sequence[Scene]) -> list[dict[str, Any]]:
    return [{"scene_id": s.scene_id, "start": _r(s.start), "end": _r(s.end)} for s in scenes]


def transcript_per_scene(
    scenes: Sequence[Scene], transcript: Transcript, max_chars: int = 300
) -> list[dict[str, Any]]:
    """Spoken text of each scene (words overlapping the scene; falls back to overlapping segments)."""
    result = []
    for scene in scenes:
        words = transcript.words_in(scene.start, scene.end)
        if words:
            text = " ".join(w.text for w in words)
        else:
            text = " ".join(
                s.text for s in transcript.segments if s.end > scene.start and s.start < scene.end
            )
        result.append({"scene_id": scene.scene_id, "text": text[:max_chars]})
    return result


def compact_analysis(analysis: VideoAnalysis, scenes: Sequence[Scene]) -> dict[str, Any]:
    """`analysis.scenes` reduced to what the planner needs, with times taken from `scenes`."""
    times = {s.scene_id: (s.start, s.end) for s in scenes}
    compact = []
    for item in analysis.scenes:
        start, end = times.get(item.scene_id, (None, None))
        compact.append(
            {
                "scene_id": item.scene_id,
                "start": None if start is None else _r(start),
                "end": None if end is None else _r(end),
                "description": item.description,
                "highlight_score": item.highlight_score,
                "role_suggestion": item.role_suggestion,
                "usable": item.usable,
                "focus_x": item.focus_x,
                "focus_y": item.focus_y,
                "problems": item.problems,
            }
        )
    return {
        "overall": analysis.overall.model_dump(),
        "scenes": compact,
        "moments": [m.model_dump() for m in analysis.moments],
    }


# the planner picks cut points INSIDE a phrase by guessing from its text; 4 s phrases keep that guess close
# (with 8 s phrases a real run still ended the hook just before the price it was meant to reveal)
PHRASE_MAX_SEC = 4.0
PHRASE_PAUSE_SEC = 0.5
_SENTENCE_END = (".", "!", "?", "…")


def transcript_phrases(transcript: Transcript) -> list[dict[str, Any]]:
    """Short phrases with EXACT times from the word timestamps. STT segments can be ~30 s long (seen on a
    real Azure transcript: 2 segments for 55 s); with only segment times the planner has to guess where
    inside such a block a sentence is, and picked the wrong sentence for the hook. A phrase ends at a
    sentence end, a pause, or PHRASE_MAX_SEC. Segments without words are passed through as they are."""
    phrases: list[dict[str, Any]] = []
    for segment in transcript.segments:
        if not segment.words:
            if segment.text.strip():
                phrases.append({"start": segment.start, "end": segment.end, "text": segment.text.strip()})
            continue
        current: list[Any] = []
        for i, word in enumerate(segment.words):
            current.append(word)
            nxt = segment.words[i + 1] if i + 1 < len(segment.words) else None
            if (
                nxt is None
                or word.text.rstrip().endswith(_SENTENCE_END)
                or nxt.start - word.end >= PHRASE_PAUSE_SEC
                or nxt.end - current[0].start > PHRASE_MAX_SEC
            ):
                text = " ".join(w.text.strip() for w in current if w.text.strip())
                phrases.append({"start": current[0].start, "end": current[-1].end, "text": text})
                current = []
    return phrases


def compact_transcript(
    transcript: Transcript, max_segments: int = 900, max_chars: int = 200
) -> list[dict[str, Any]]:
    """Phrases (see transcript_phrases) merged into at most `max_segments` groups. `max_chars` is per
    phrase: a merged group may hold `max_chars` x its phrase count, so merging never drops speech."""
    phrases = transcript_phrases(transcript)
    if not phrases:
        return []
    group = max(1, math.ceil(len(phrases) / max_segments))
    merged = []
    for i in range(0, len(phrases), group):
        chunk = phrases[i : i + group]
        text = " ".join(ph["text"] for ph in chunk)
        merged.append(
            {
                "start": _r(chunk[0]["start"]),
                "end": _r(chunk[-1]["end"]),
                "text": text[: max_chars * len(chunk)],
            }
        )
    return merged


@dataclass(slots=True)
class PlanContext:
    """Everything the planner needs about one video (built by the worker after P06 + analysis)."""

    source: SourceInfo
    analysis: VideoAnalysis
    scenes: list[Scene]
    transcript: Transcript
    silences: list[Silence]
    music_tracks: list[dict[str, Any]] = field(default_factory=list)  # [{"id","mood"}]
    creator_profile: dict[str, str] = field(default_factory=dict)  # {"niche","purpose"}
    broll_sources: list[BrollSourceInfo] = field(default_factory=list)  # P13: optional cutaway sources

    @property
    def music_ids(self) -> set[str]:
        return {str(t["id"]) for t in self.music_tracks}

    @property
    def source_durations(self) -> dict[str, float]:
        """`{source_id: duration_sec}` for every source a `Clip.source_id` may reference (P13)."""
        durations = {"primary": self.source.duration_sec}
        durations.update({b.source_id: b.duration_sec for b in self.broll_sources})
        return durations

    def _shared(self, job: dict[str, Any], preset: PresetRules) -> dict[str, Any]:
        data = {
            "source": self.source.as_dict(),
            "job": job,
            "preset_rules": preset_rules_dict(preset),
            "transcript_segments": compact_transcript(self.transcript),
            "silences": [{"start": _r(s.start), "end": _r(s.end)} for s in self.silences],
            "music_tracks": [{"id": t["id"], "mood": t.get("mood", "")} for t in self.music_tracks],
        }
        if self.broll_sources:
            data["broll_sources"] = [b.as_dict() for b in self.broll_sources]
        return data

    def planner_input(self, job: dict[str, Any], preset: PresetRules) -> dict[str, Any]:
        """User message of the planner (B.2)."""
        data = self._shared(job, preset)
        data["creator_profile"] = self.creator_profile
        data["analysis"] = compact_analysis(self.analysis, self.scenes)
        return data

    def revision_context(self, job: dict[str, Any], preset: PresetRules) -> dict[str, Any]:
        """`context` of the revision message (C.2)."""
        data = self._shared(job, preset)
        data["analysis_compact"] = compact_analysis(self.analysis, self.scenes)
        return data
