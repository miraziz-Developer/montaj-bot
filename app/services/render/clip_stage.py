"""Render stage A: one re-encoded file per clip (docs/EDIT_PLAN_SCHEMA.md section 6)."""

import logging
from pathlib import Path

from app.schemas.edit_plan import Clip, Reframe
from app.services.media.ffmpeg import run_ffmpeg
from app.services.media.probe import probe

logger = logging.getLogger(__name__)


def _even(value: float) -> int:
    n = int(round(value))
    return n if n % 2 == 0 else n + 1


def fill_chain(target_w: int, target_h: int, reframe: Reframe) -> str:
    """Cover the target at zoom z, then crop around the focus point.

    Every number is computed here in Python; only ffmpeg's own `min`/`max` expression functions appear in
    the filter (consistent approach). The quotes keep the commas of the expressions out of the filter parser.
    """
    scaled_w, scaled_h = _even(target_w * reframe.zoom), _even(target_h * reframe.zoom)
    half_w, half_h = f"{target_w / 2:g}", f"{target_h / 2:g}"
    x = f"'min(max(iw*{reframe.focus_x:.4f}-{half_w},0),iw-{target_w})'"
    y = f"'min(max(ih*{reframe.focus_y:.4f}-{half_h},0),ih-{target_h})'"
    return (
        f"scale=w={scaled_w}:h={scaled_h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
        f"crop={target_w}:{target_h}:x={x}:y={y}"
    )


def fit_blur_graph(target_w: int, target_h: int, speed: float, fps: int) -> str:
    """Whole frame visible over a blurred, cropped copy of itself. `W`,`H`,`w`,`h` are ffmpeg variables."""
    w, h = target_w, target_h
    return (
        f"[0:v]split=2[a][b];"
        f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5[bg];"
        f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setpts=PTS/{speed:g},fps={fps},format=yuv420p[v]"
    )


def audio_filter(clip: Clip) -> str:
    parts = []
    if clip.speed != 1.0:
        parts.append(f"atempo={clip.speed:g}")  # one instance covers the schema's 0.5..2.0 range
    parts.append(f"volume={0 if clip.audio.mute else clip.audio.volume:g}")
    parts += ["aresample=48000", "aformat=channel_layouts=stereo"]
    return ",".join(parts)


async def render_clip(
    source: Path,
    clip: Clip,
    *,
    out_path: Path,
    target_w: int,
    target_h: int,
    fps: int,
    has_audio: bool | None = None,
    preset: str = "veryfast",
    crf: int = 21,
    audio_bitrate_k: int = 160,
    timeout: float = 1800,
) -> None:
    """Cut [src_in, src_out] out of `source`, reframe to target_w x target_h, apply speed/volume, re-encode.

    Input seeking + re-encode keeps the cut frame-accurate. A source without audio gets a silent stereo
    track (`anullsrc`) so that every clip has identical streams and the join can use `-c copy`.
    """
    if has_audio is None:
        has_audio = (await probe(str(source))).has_audio
    src_dur = clip.src_out - clip.src_in
    out_dur = clip.out_duration

    args = [
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{clip.src_in:.3f}",
        "-t",
        f"{src_dur:.3f}",
        "-i",
        str(source),
    ]
    if not has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]

    if clip.reframe.mode == "fit_blur":
        args += ["-filter_complex", fit_blur_graph(target_w, target_h, clip.speed, fps), "-map", "[v]"]
    else:
        chain = fill_chain(target_w, target_h, clip.reframe)
        args += ["-vf", f"{chain},setpts=PTS/{clip.speed:g},fps={fps},format=yuv420p", "-map", "0:v:0"]
    args += ["-af", audio_filter(clip), "-map", "0:a:0" if has_audio else "1:a:0", "-t", f"{out_dur:.3f}"]
    args += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high", "-g", str(2 * fps),
        "-c:a", "aac", "-b:a", f"{audio_bitrate_k}k", "-ar", "48000", "-ac", "2",
        str(out_path),
    ]  # fmt: skip
    await run_ffmpeg(args, timeout=timeout, cwd=out_path.parent)
