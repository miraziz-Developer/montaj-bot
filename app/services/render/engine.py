"""Render pipeline: EditPlan + source + transcript -> final.mp4 (EDIT_PLAN_SCHEMA.md sections 2, 6)."""

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import RenderError
from app.schemas.edit_plan import EditPlan
from app.services.media.probe import looks_like_round_video_note, probe
from app.services.render.captions_ass import build_ass, build_timeline, has_events
from app.services.render.clip_stage import render_clip
from app.services.render.concat_stage import concat_clips
from app.services.render.final_stage import render_final
from app.services.render.music import resolve_music_track
from app.services.stt.base import Transcript

logger = logging.getLogger(__name__)

DURATION_TOLERANCE = 0.10
MIN_TOLERANCE_SEC = 0.5


@dataclass(frozen=True, slots=True)
class RenderStats:
    duration_sec: float
    render_seconds: float
    size_bytes: int


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def output_resolution(
    aspect: str, source_w: int, source_h: int, *, max_short_side: int = 1080
) -> tuple[int, int]:
    """Output size (both dimensions even). Fixed aspects use a short side of min(1080, max_short_side);
    "original" keeps the source aspect with its short side clamped to `max_short_side`."""
    short = min(1080, max_short_side)
    if aspect == "9:16":
        return _even(short), _even(short * 16 / 9)
    if aspect == "16:9":
        return _even(short * 16 / 9), _even(short)
    if aspect == "1:1":
        return _even(short), _even(short)
    if aspect == "original":
        scale = min(1.0, max_short_side / min(source_w, source_h))
        return _even(source_w * scale), _even(source_h * scale)
    raise ValueError(f"unknown aspect {aspect!r}")


async def render_plan(
    *,
    source: Path | None = None,
    sources: dict[str, Path] | None = None,
    plan: EditPlan,
    transcript: Transcript,
    workdir: Path,
    out_path: Path,
    assets_dir: Path,
    max_short_side: int = 1080,
    clip_concurrency: int | None = None,
) -> RenderStats:
    """Render `plan` to `out_path`. Per-clip temp files are always removed; `workdir` is the caller's.

    Pass `source` (single-source jobs, unchanged) or `sources` (P13: `{"primary": path, "broll_1": path,
    ...}`) - exactly one of the two. Every clip's `source_id` looks itself up in `sources`; a clip whose
    `audio.source == "primary"` additionally pulls its audio track from `sources["primary"]`."""
    assert (source is None) != (sources is None), "pass exactly one of source/sources"
    sources = sources or {"primary": source}  # type: ignore[dict-item]
    started = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    clips_dir = workdir / "clips"
    clips_dir.mkdir(exist_ok=True)
    joined = workdir / "joined.mp4"
    concurrency = clip_concurrency or get_settings().render_clip_concurrency
    shake_fix = get_settings().render_stabilize

    infos = {source_id: await probe(str(path)) for source_id, path in sources.items()}
    info = infos["primary"]
    width, height = output_resolution(
        plan.target.aspect, info.width, info.height, max_short_side=max_short_side
    )
    fps = plan.target.fps
    semaphore = asyncio.Semaphore(max(1, concurrency))
    clip_paths = [clips_dir / f"clip_{i:04d}.mp4" for i in range(1, len(plan.clips) + 1)]

    async def cut(clip, path: Path) -> None:  # noqa: ANN001
        async with semaphore:
            clip_source = sources[clip.source_id]
            clip_info = infos[clip.source_id]
            await render_clip(
                clip_source, clip, out_path=path, target_w=width, target_h=height, fps=fps,
                has_audio=clip_info.has_audio, audio_duration_sec=clip_info.audio_duration_sec,
                audio_source=sources["primary"] if clip.audio.source == "primary" else None,
                round_note=looks_like_round_video_note(clip_info.width, clip_info.height),
                hdr=clip_info.is_hdr, sar=clip_info.sar, shake_fix=shake_fix,
                preset=plan.export.preset, crf=plan.export.crf,
                audio_bitrate_k=plan.export.audio_bitrate_k,
            )  # fmt: skip

    try:
        await asyncio.gather(*(cut(c, p) for c, p in zip(plan.clips, clip_paths, strict=True)))
        clips_done = time.monotonic()
        await concat_clips(clip_paths, out_path=joined, workdir=workdir, clips=plan.clips)
        joined_done = time.monotonic()

        timeline = build_timeline(plan.clips)
        ass_path: Path | None = None
        ass_text = build_ass(plan, transcript, timeline, target_w=width, target_h=height)
        if has_events(ass_text):  # captions, overlays or a watermark exist
            ass_path = workdir / "captions.ass"
            ass_path.write_text(ass_text, encoding="utf-8")

        fonts_dir = workdir / "fonts"
        source_fonts = assets_dir / "fonts"
        if source_fonts.is_dir():
            await asyncio.to_thread(shutil.copytree, source_fonts, fonts_dir, dirs_exist_ok=True)

        music_path = resolve_music_track(plan.music.track_id, assets_dir) if plan.music.enabled else None
        await render_final(
            joined=joined, ass_path=ass_path, music_path=music_path, plan=plan,
            target_w=width, target_h=height, out_path=out_path, fonts_dir=fonts_dir,
        )  # fmt: skip
        logger.info(
            "render stages clips=%.1fs join=%.1fs final=%.1fs (%d clips)",
            clips_done - started, joined_done - clips_done, time.monotonic() - joined_done, len(plan.clips),
        )  # fmt: skip
    finally:
        shutil.rmtree(clips_dir, ignore_errors=True)
        joined.unlink(missing_ok=True)

    result = await probe(str(out_path))
    expected = plan.total_duration()
    if abs(result.duration_sec - expected) > max(DURATION_TOLERANCE * expected, MIN_TOLERANCE_SEC):
        raise RenderError(
            f"output duration {result.duration_sec:.2f}s differs from the plan ({expected:.2f}s)"
        )
    stats = RenderStats(
        duration_sec=result.duration_sec,
        render_seconds=time.monotonic() - started,
        size_bytes=out_path.stat().st_size,
    )
    logger.info(
        "render done duration=%.1fs took=%.1fs size=%s",
        stats.duration_sec,
        stats.render_seconds,
        stats.size_bytes,
    )
    return stats
