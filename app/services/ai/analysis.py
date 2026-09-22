"""Full-video analysis: split into windows, call the LLM per window, merge."""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from app.schemas.analysis import KeyMoment, SceneAnalysis, VideoAnalysis
from app.services.ai.llm import LLMClient, UsageInfo
from app.services.ai.plan_text import scene_dicts, transcript_per_scene
from app.services.media.scenes import Scene
from app.services.stt.base import Transcript

logger = logging.getLogger(__name__)


def split_windows(scenes: Sequence[Scene], chunk_sec: float) -> list[list[Scene]]:
    """Consecutive scenes grouped into windows of <= chunk_sec (a scene is never split across windows)."""
    windows: list[list[Scene]] = []
    current: list[Scene] = []
    for scene in scenes:
        if current and scene.end - current[0].start > chunk_sec:
            windows.append(current)
            current = []
        current.append(scene)
    if current:
        windows.append(current)
    return windows


def _normalize(
    analysis: VideoAnalysis, window: Sequence[Scene]
) -> tuple[list[SceneAnalysis], list[KeyMoment]]:
    """Exactly one entry per requested scene, in order: drop unknown ids, fill in missing ones."""
    wanted = [s.scene_id for s in window]
    by_id: dict[int, SceneAnalysis] = {}
    for item in analysis.scenes:
        if item.scene_id in wanted and item.scene_id not in by_id:
            by_id[item.scene_id] = item
    missing = [i for i in wanted if i not in by_id]
    if missing:
        logger.warning("analysis missed scenes %s: using neutral defaults", missing)
    scenes = [by_id.get(i) or SceneAnalysis(scene_id=i) for i in wanted]
    moments = [m for m in analysis.moments if m.scene_id in wanted]
    return scenes, moments


async def analyze_full_video(
    gemini: LLMClient,
    *,
    proxy_path: Path,
    scenes: Sequence[Scene],
    transcript: Transcript,
    niche: str,
    purpose: str,
    chunk_sec: float,
    max_concurrency: int = 2,
    fps: float | None = None,
) -> tuple[VideoAnalysis, UsageInfo]:
    windows = split_windows(scenes, chunk_sec)
    if not windows:
        return VideoAnalysis(scenes=[]), UsageInfo()
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def run(window: list[Scene]) -> tuple[VideoAnalysis, UsageInfo]:
        async with semaphore:
            return await gemini.analyze_video(
                video_path=proxy_path,
                window=(window[0].start, window[-1].end),
                scenes=scene_dicts(window),
                transcript_by_scene=transcript_per_scene(window, transcript),
                niche=niche,
                purpose=purpose,
                fps=fps,
            )

    try:
        results = await asyncio.gather(*(run(w) for w in windows))
    finally:
        try:
            await gemini.release_video(proxy_path)
        except Exception:
            logger.warning("release_video failed", exc_info=True)

    merged_scenes: list[SceneAnalysis] = []
    merged_moments: list[KeyMoment] = []
    usage = UsageInfo()
    for window, (analysis, used) in zip(windows, results, strict=True):
        window_scenes, window_moments = _normalize(analysis, window)
        merged_scenes += window_scenes
        merged_moments += window_moments
        usage += used

    # LIMITATION: `overall` comes from the FIRST window only. It describes the whole video only when the
    # video fits one window; for long videos it is a good-enough summary of the opening (MVP).
    overall = results[0][0].overall.model_copy(
        update={"has_speech": any(r[0].overall.has_speech for r in results)}
    )
    return VideoAnalysis(overall=overall, scenes=merged_scenes, moments=merged_moments), usage
