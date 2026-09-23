import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.errors import InvalidMedia
from app.services.media.ffmpeg import FFmpegError, run, run_ffprobe

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SEC = 60


@dataclass(frozen=True, slots=True)
class ProbeResult:
    duration_sec: float
    width: int
    height: int
    fps: float | None
    has_audio: bool
    video_codec: str | None
    size_bytes: int | None
    format_name: str | None
    audio_duration_sec: float | None = None  # None: no audio stream, or its duration was unreadable


def looks_like_round_video_note(width: int | None, height: int | None) -> bool:
    """Telegram "video note" round messages (and clips re-saved from them) are always exactly square and
    small, with the real picture circle-masked - corner pixels outside the circle are baked in (white or
    black). A small square is the only cheap, reliable signal without pixel analysis: genuine square
    footage from a phone or camera is essentially never this small, but a video note commonly is
    (Telegram renders these around 240-640px)."""
    if not width or not height:
        return False
    is_square = abs(width - height) / max(width, height) < 0.02
    is_small = min(width, height) <= 640
    return is_square and is_small


def _parse_fps(stream: dict[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(stream.get(key, "")).partition("/")
        try:
            value = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return round(value, 3)
    return None


def parse_probe_output(raw_json: str) -> ProbeResult:
    """Turn `ffprobe -print_format json` output into a ProbeResult; raise InvalidMedia if unusable."""
    try:
        data = json.loads(raw_json)
        streams = data.get("streams") or []
        fmt = data.get("format") or {}
        video = next(
            (
                s
                for s in streams
                if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
            ),
            None,
        )
        if video is None:
            raise InvalidMedia()
        duration = float(fmt.get("duration") or video.get("duration") or 0)
        width, height = int(video["width"]), int(video["height"])
        size = int(fmt["size"]) if fmt.get("size") is not None else None
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        audio_duration = None
        if audio is not None:
            try:
                audio_duration = float(audio["duration"])
            except (KeyError, ValueError, TypeError):
                audio_duration = None  # some containers omit per-stream duration; treat as "unknown"
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InvalidMedia() from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise InvalidMedia()
    return ProbeResult(
        duration_sec=duration,
        width=width,
        height=height,
        fps=_parse_fps(video),
        has_audio=audio is not None,
        video_codec=video.get("codec_name"),
        size_bytes=size,
        format_name=fmt.get("format_name"),
        audio_duration_sec=audio_duration,
    )


async def media_duration(path: Path | str) -> float:
    """Duration in seconds of any media file (audio-only included). Raises InvalidMedia if unreadable."""
    try:
        _, stdout, _ = await run_ffprobe(
            ["-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)]
        )
        duration = float(stdout.strip())
    except (FFmpegError, ValueError) as exc:
        raise InvalidMedia() from exc
    if not math.isfinite(duration) or duration <= 0:
        raise InvalidMedia()
    return duration


async def probe(source: str) -> ProbeResult:
    """ffprobe a local path or an https URL (SAS URLs are redacted from any error text)."""
    args = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", source]
    try:
        _, stdout, _ = await run(args, timeout=PROBE_TIMEOUT_SEC)
    except FFmpegError as exc:
        logger.warning("ffprobe failed: %s", exc)
        raise InvalidMedia() from exc
    return parse_probe_output(stdout)
