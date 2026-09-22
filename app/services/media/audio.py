import logging
import os
from dataclasses import dataclass
from pathlib import Path

from app.services.media.ffmpeg import run_ffmpeg
from app.services.media.probe import probe

logger = logging.getLogger(__name__)

MIN_CHUNK_SEC = 0.2  # a shorter trailing sliver carries nothing worth transcribing


@dataclass(frozen=True, slots=True)
class AudioChunk:
    path: Path
    start_sec: float
    end_sec: float


def chunk_ranges(duration_sec: float, chunk_sec: int) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    start = 0.0
    while duration_sec - start >= MIN_CHUNK_SEC:
        end = min(start + chunk_sec, duration_sec)
        ranges.append((start, end))
        start = end
    return ranges


async def extract_audio_chunks(
    source: Path,
    out_dir: Path,
    *,
    chunk_sec: int,
    has_audio: bool | None = None,
    duration_sec: float | None = None,
    timeout: float = 900,
) -> list[AudioChunk]:
    """Mono 16 kHz Opus/OGG chunks of <= chunk_sec. Returns [] (no ffmpeg call) if there is no audio.

    `has_audio`/`duration_sec` avoid a second ffprobe when the caller already knows them.
    Chunks are re-encoded (not stream-copied) for exact boundaries; existing chunk files are reused.
    """
    if has_audio is None or duration_sec is None:
        info = await probe(str(source))
        has_audio, duration_sec = info.has_audio, info.duration_sec
    if not has_audio:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[AudioChunk] = []
    for index, (start, end) in enumerate(chunk_ranges(duration_sec, chunk_sec)):
        path = out_dir / f"audio_{index:03d}.ogg"
        if not (path.exists() and path.stat().st_size > 0):
            partial = path.with_name(f".{path.name}.part.ogg")
            args = [
                "-y",
                "-ss", f"{start:.3f}",
                "-t", f"{end - start:.3f}",
                "-i", str(source),
                "-vn", "-map", "0:a:0",
                "-ac", "1", "-ar", "16000",
                "-c:a", "libopus", "-b:a", "32k",
                str(partial),
            ]  # fmt: skip
            try:
                await run_ffmpeg(args, timeout=timeout)
                os.replace(partial, path)
            finally:
                partial.unlink(missing_ok=True)
        chunks.append(AudioChunk(path=path, start_sec=start, end_sec=end))
    logger.info("audio chunks extracted count=%s", len(chunks))
    return chunks
