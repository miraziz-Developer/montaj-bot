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
    # width/height above are DISPLAY size: phones store landscape pixels plus a rotation flag, and ffmpeg
    # auto-rotates on decode, so a portrait phone video is 1080x1920 here even though it is stored 1920x1080.
    rotation: int = 0  # display rotation in degrees (0/90/180/270)
    is_hdr: bool = False  # PQ/HLG transfer: must be tone-mapped to SDR or it renders washed out


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


_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def _display_rotation(video: dict[str, Any]) -> int:
    """Rotation the player applies, normalised to 0/90/180/270. Modern files carry a Display Matrix side
    data entry (negative = clockwise in ffprobe's convention); old ones carry a `rotate` tag."""
    raw: Any = (video.get("tags") or {}).get("rotate")
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            raw = side["rotation"]
    try:
        return int(round(float(raw))) % 360 if raw is not None else 0
    except (TypeError, ValueError):
        return 0


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
        rotation = _display_rotation(video)
        if rotation in (90, 270):
            width, height = height, width
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
        rotation=rotation,
        is_hdr=video.get("color_transfer") in _HDR_TRANSFERS,
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
