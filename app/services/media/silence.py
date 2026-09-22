import re
from dataclasses import dataclass
from pathlib import Path

from app.services.media.ffmpeg import run_ffmpeg
from app.services.media.probe import media_duration

_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True, slots=True)
class Silence:
    start: float
    end: float


def parse_silencedetect(stderr: str, duration: float) -> list[Silence]:
    """Parse `silencedetect` log lines. A silence still open at EOF is closed at `duration`."""
    silences: list[Silence] = []
    open_start: float | None = None
    for line in stderr.splitlines():
        if (match := _START.search(line)) is not None:
            open_start = max(0.0, float(match.group(1)))
        elif (match := _END.search(line)) is not None and open_start is not None:
            end = min(duration, float(match.group(1)))
            if end > open_start:
                silences.append(Silence(round(open_start, 3), round(end, 3)))
            open_start = None
    if open_start is not None and duration > open_start:
        silences.append(Silence(round(open_start, 3), round(duration, 3)))
    return silences


async def detect_silences(
    source: Path, *, noise_db: float = -30.0, min_duration: float = 0.3, timeout: float = 1800
) -> list[Silence]:
    """Silent intervals of the audio track of `source` (audio or video file; video is not decoded)."""
    _, _, stderr = await run_ffmpeg(
        [
            "-i",
            str(source),
            "-vn",
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ],  # fmt: skip
        timeout=timeout,
    )
    return parse_silencedetect(stderr, await media_duration(source))
