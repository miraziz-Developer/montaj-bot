import logging
import os
from pathlib import Path

from app.services.media.ffmpeg import run_ffmpeg

logger = logging.getLogger(__name__)


def scale_filter(short_side: int) -> str:
    """Scale so the SHORT side equals `short_side` (never upscale), keeping both dimensions even.

    Quoted expressions keep the commas inside them from being read as filter separators.
    """
    s = short_side
    return f"scale=w='if(gt(iw,ih),-2,trunc(min(iw,{s})/2)*2)':h='if(gt(iw,ih),trunc(min(ih,{s})/2)*2,-2)'"


async def make_proxy(
    source: Path, dest: Path, *, short_side: int, fps: int = 30, timeout: float = 3600
) -> None:
    """Small h264/aac proxy for analysis. Written to a temp file first, so `dest` is never half-written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(f".{dest.name}.part.mp4")
    args = [
        "-y",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-sn", "-dn",
        "-vf", scale_filter(short_side),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-g", str(2 * fps),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "96k",
        "-ar", "48000",
        "-movflags", "+faststart",
        str(partial),
    ]  # fmt: skip
    try:
        await run_ffmpeg(args, timeout=timeout)
        os.replace(partial, dest)
    finally:
        partial.unlink(missing_ok=True)
    logger.info("proxy created short_side=%s size=%s", short_side, dest.stat().st_size)
