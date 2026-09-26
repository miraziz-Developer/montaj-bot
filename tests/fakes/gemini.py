"""FakeGeminiClient (LLMClient level) plus builders for plans/transcripts used across the AI tests."""

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.core.errors import AIError
from app.schemas.analysis import KeyMoment, OverallInfo, SceneAnalysis, VideoAnalysis
from app.schemas.edit_plan import Clip, EditPlan
from app.services.ai.llm import UsageInfo
from app.services.stt.base import Transcript, TranscriptSegment, Word


def make_plan(
    ranges: Sequence[tuple[float, float]], style: str = "dynamic_reels", aspect: str = "9:16", **fields: Any
) -> EditPlan:
    clips = [Clip(id=f"c{i}", src_in=a, src_out=b) for i, (a, b) in enumerate(ranges, start=1)]
    return EditPlan(
        title="Test",
        style_preset=style,  # type: ignore[arg-type]
        target={"aspect": aspect},  # type: ignore[arg-type]
        clips=clips,
        **fields,
    )


def make_transcript(words: Sequence[tuple[str, float, float]], segment_gap: float = 0.6) -> Transcript:
    """One segment per run of words separated by less than `segment_gap` seconds."""
    segments: list[TranscriptSegment] = []
    current: list[Word] = []
    for text, start, end in words:
        if current and start - current[-1].end >= segment_gap:
            segments.append(_segment(current))
            current = []
        current.append(Word(text=text, start=start, end=end))
    if current:
        segments.append(_segment(current))
    return Transcript(language="uz", segments=segments)


def _segment(words: list[Word]) -> TranscriptSegment:
    return TranscriptSegment(
        start=words[0].start, end=words[-1].end, text=" ".join(w.text for w in words), words=list(words)
    )


def speech(start: float, end: float, step: float = 0.5) -> list[tuple[str, float, float]]:
    """Evenly spaced words (0.4 s long, 0.1 s apart) covering [start, end]."""
    words, t, i = [], start, 0
    while t + 0.4 <= end + 1e-9:
        words.append((f"w{i}", round(t, 3), round(t + 0.4, 3)))
        t += step
        i += 1
    return words


class FakeGeminiClient:
    def __init__(
        self,
        *,
        plan: EditPlan | None = None,
        plan_error: AIError | None = None,
        revision: tuple[EditPlan, list[str], list[str]] | None = None,
        revise_error: AIError | None = None,
        analysis_factory: Callable[[list[dict[str, Any]]], VideoAnalysis] | None = None,
        analysis_delay: float = 0.0,
    ) -> None:
        self.plan_result, self.plan_error = plan, plan_error
        self.revision, self.revise_error = revision, revise_error
        self.analysis_factory = analysis_factory
        self.analysis_delay = analysis_delay
        self.analyze_calls: list[dict[str, Any]] = []
        self.plan_contexts: list[dict[str, Any]] = []
        self.revise_calls: list[dict[str, Any]] = []
        self.released: list[Path] = []
        self.transcript_fix: Callable[[str], str] | None = None  # draft -> corrected (None: unchanged)
        self.fix_calls: list[str] = []
        self._running = 0
        self.max_running = 0

    async def analyze_video(self, **kwargs: Any) -> tuple[VideoAnalysis, UsageInfo]:
        self.analyze_calls.append(kwargs)
        self._running += 1
        self.max_running = max(self.max_running, self._running)
        try:
            if self.analysis_delay:
                await asyncio.sleep(self.analysis_delay)
            scenes = kwargs["scenes"]
            if self.analysis_factory:
                return self.analysis_factory(scenes), UsageInfo(100, 20)
            return default_analysis(scenes), UsageInfo(100, 20)
        finally:
            self._running -= 1

    async def plan(self, *, context: dict[str, Any]) -> tuple[EditPlan, UsageInfo]:
        self.plan_contexts.append(context)
        if self.plan_error:
            raise self.plan_error
        assert self.plan_result is not None
        return self.plan_result.model_copy(deep=True), UsageInfo(1000, 200)

    async def revise(
        self, *, current_plan: EditPlan, message: str, context: dict[str, Any]
    ) -> tuple[EditPlan, list[str], list[str], UsageInfo]:
        self.revise_calls.append({"current_plan": current_plan, "message": message, "context": context})
        if self.revise_error:
            raise self.revise_error
        assert self.revision is not None
        plan, changes, unsupported = self.revision
        return plan.model_copy(deep=True), list(changes), list(unsupported), UsageInfo(500, 100)

    async def correct_transcript(self, *, audio_path: Path, draft: str) -> tuple[str, UsageInfo]:
        assert audio_path.is_file(), "the corrector must receive a real audio file"
        self.fix_calls.append(draft)
        return (self.transcript_fix(draft) if self.transcript_fix else draft), UsageInfo(50, 10)

    async def release_video(self, video_path: Path) -> None:
        self.released.append(video_path)


def default_analysis(scenes: Sequence[dict[str, Any]], has_speech: bool = True) -> VideoAnalysis:
    return VideoAnalysis(
        overall=OverallInfo(summary="test", has_speech=has_speech),
        scenes=[SceneAnalysis(scene_id=s["scene_id"], description=f"scene {s['scene_id']}") for s in scenes],
        moments=[KeyMoment(scene_id=scenes[0]["scene_id"], type="hook_candidate")] if scenes else [],
    )
