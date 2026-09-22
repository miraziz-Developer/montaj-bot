import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from scenedetect import ContentDetector, SceneManager, open_video

from app.services.media.silence import Silence

logger = logging.getLogger(__name__)

MAX_SCENE_SEC = 12.0
SPLIT_STEP_SEC = 8.0


@dataclass(frozen=True, slots=True)
class Scene:
    scene_id: int  # 1-indexed
    start: float
    end: float


def _merge_short(scenes: list[tuple[float, float]], min_len: float) -> list[tuple[float, float]]:
    """Merge scenes shorter than `min_len` into the next one (into the previous one if it is the last)."""
    scenes = list(scenes)
    while len(scenes) > 1:
        idx = next((i for i, (a, b) in enumerate(scenes) if b - a < min_len), None)
        if idx is None:
            break
        if idx < len(scenes) - 1:
            scenes[idx : idx + 2] = [(scenes[idx][0], scenes[idx + 1][1])]
        else:
            scenes[idx - 1 : idx + 1] = [(scenes[idx - 1][0], scenes[idx][1])]
    return scenes


def _split_long(
    start: float, end: float, silences: Sequence[Silence], min_len: float
) -> list[tuple[float, float]]:
    """Split a scene longer than 12 s: prefer a silence midpoint, else the nearest 8 s boundary."""
    midpoints = [(s.start + s.end) / 2 for s in silences]
    pieces: list[tuple[float, float]] = []
    cursor = start
    while end - cursor > MAX_SCENE_SEC:
        candidates = [
            m for m in midpoints if cursor + min_len <= m <= min(cursor + MAX_SCENE_SEC, end - min_len)
        ]
        if candidates:
            cut = min(candidates, key=lambda m: abs(m - (cursor + SPLIT_STEP_SEC)))
        else:
            cut = cursor + SPLIT_STEP_SEC
            if end - cut < min_len:
                cut = (cursor + end) / 2
        pieces.append((cursor, cut))
        cursor = cut
    pieces.append((cursor, end))
    return pieces


def postprocess_scenes(
    cuts: Sequence[float], duration: float, silences: Sequence[Silence], min_scene_len_sec: float
) -> list[Scene]:
    """Turn raw cut times into final scenes (merge slivers, split long ones, renumber from 1).

    No cuts (e.g. a static talking head) -> one scene for the whole video, then split by the 12 s rule.
    """
    inner = sorted({round(c, 3) for c in cuts if 0 < c < duration})
    edges = [0.0, *inner, duration]
    scenes = _merge_short(list(zip(edges, edges[1:], strict=False)), min_scene_len_sec)
    final: list[tuple[float, float]] = []
    for start, end in scenes:
        final.extend(_split_long(start, end, silences, min_scene_len_sec))
    return [Scene(i, round(a, 3), round(b, 3)) for i, (a, b) in enumerate(final, start=1)]


def _detect_cuts_sync(path: Path, min_scene_len_sec: float) -> tuple[list[float], float]:
    # Verified against scenedetect 0.7.1: open_video / SceneManager.detect_scenes / get_scene_list
    video = open_video(str(path))
    fps = float(video.frame_rate)
    duration = video.duration.seconds
    manager = SceneManager()
    manager.add_detector(ContentDetector(min_scene_len=max(1, int(fps * min_scene_len_sec))))
    manager.detect_scenes(video)
    scene_list = manager.get_scene_list()
    return [start.seconds for start, _ in scene_list[1:]], duration


async def detect_scenes(
    proxy_path: Path, *, silences: Sequence[Silence] = (), min_scene_len_sec: float = 1.0
) -> list[Scene]:
    cuts, duration = await asyncio.to_thread(_detect_cuts_sync, proxy_path, min_scene_len_sec)
    scenes = postprocess_scenes(cuts, duration, silences, min_scene_len_sec)
    logger.info("scenes detected raw_cuts=%s final=%s", len(cuts), len(scenes))
    return scenes
