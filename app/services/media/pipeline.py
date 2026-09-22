"""Deterministic pre-analysis: proxy, audio chunks, transcript, silences, scenes (no LLM).

Idempotent per ARCHITECTURE section 10: every artifact already present in Blob is reused, so a retried
job does not recompute (or re-pay for) finished stages.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.models.job import Job
from app.models.upload import Upload
from app.services.media.audio import AudioChunk, extract_audio_chunks
from app.services.media.proxy import make_proxy
from app.services.media.scenes import Scene, detect_scenes
from app.services.media.silence import Silence, detect_silences
from app.services.storage import BlobStorage
from app.services.stt.base import STTProvider, Transcript

logger = logging.getLogger(__name__)

AUDIO_CHUNK_SEC = 600
PROXY_SHORT_SIDE = 720
PROXY_SHORT_SIDE_LONG = 480


@dataclass(frozen=True, slots=True)
class PreAnalysisResult:
    proxy_path: Path
    transcript: Transcript
    silences: list[Silence]
    scenes: list[Scene]
    chunks: list[AudioChunk]  # empty when the transcript was reused from a previous run


class ArtifactStore:
    """Blob helpers for one job's artifacts."""

    def __init__(self, storage: BlobStorage, container: str, job_id: object, workdir: Path) -> None:
        self.storage, self.container, self.prefix, self.workdir = storage, container, str(job_id), workdir

    def blob(self, name: str) -> str:
        return f"{self.prefix}/{name}"

    async def exists(self, name: str) -> bool:
        return await self.storage.get_blob_size(self.container, self.blob(name)) is not None

    async def upload(self, name: str, path: Path, content_type: str) -> None:
        await self.storage.upload_file(self.container, self.blob(name), path, content_type)

    async def load_json(self, name: str) -> Any | None:
        if not await self.exists(name):
            return None
        local = self.workdir / f"cached_{name}"
        await self.storage.download_to_file(self.container, self.blob(name), local)
        try:
            return json.loads(local.read_text())
        except (ValueError, OSError):
            logger.warning("cached artifact %s is unreadable: recomputing", name)
            return None

    async def save_json(self, name: str, data: Any) -> None:
        local = self.workdir / name
        local.write_text(json.dumps(data, ensure_ascii=False))
        await self.upload(name, local, "application/json")


async def run_pre_analysis(
    job: Job,
    upload: Upload,
    workdir: Path,
    storage: BlobStorage,
    stt: STTProvider,
    settings: Settings,
    *,
    audio_chunk_sec: int = AUDIO_CHUNK_SEC,
    language_hint: str | None = None,
) -> PreAnalysisResult:
    """Download the source if needed, then produce proxy / transcript / silences / scenes.

    Artifacts are uploaded to the `artifacts` container under `{job_id}/`. The caller owns `workdir`
    and removes it in a `finally` block.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    art = ArtifactStore(storage, settings.azure_artifacts_container, job.id, workdir)
    source = workdir / f"source{Path(upload.blob_path).suffix}"
    has_audio = bool(upload.has_audio)
    duration = float(upload.duration_sec or 0)

    async def ensure_source() -> Path:
        if not source.exists():
            await storage.download_to_file(settings.azure_uploads_container, upload.blob_path, source)
        return source

    # 1. proxy
    proxy_path = workdir / "proxy.mp4"
    proxy_in_blob = await art.exists("proxy.mp4")
    if not proxy_path.exists():
        if proxy_in_blob:
            await storage.download_to_file(art.container, art.blob("proxy.mp4"), proxy_path)
        else:
            short_side = (
                PROXY_SHORT_SIDE_LONG if duration > settings.long_video_threshold_sec else PROXY_SHORT_SIDE
            )
            await make_proxy(await ensure_source(), proxy_path, short_side=short_side)
    if not proxy_in_blob:
        await art.upload("proxy.mp4", proxy_path, "video/mp4")

    # 2. transcript (audio chunks -> STT, bounded concurrency)
    chunks: list[AudioChunk] = []
    cached = await art.load_json("transcript.json")
    if cached is not None:
        transcript = Transcript.model_validate(cached)
    else:
        chunks = await extract_audio_chunks(
            await ensure_source(),
            workdir / "audio",
            chunk_sec=audio_chunk_sec,
            has_audio=has_audio,
            duration_sec=duration or None,
        )
        semaphore = asyncio.Semaphore(max(1, settings.stt_concurrency))

        async def transcribe(chunk: AudioChunk) -> Transcript:
            async with semaphore:
                part = await stt.transcribe(chunk.path, language_hint=language_hint)
            return part.offset(chunk.start_sec)

        parts = await asyncio.gather(*(transcribe(c) for c in chunks))
        transcript = Transcript.merge(parts) if parts else Transcript(language="", segments=[])
        await art.save_json("transcript.json", transcript.model_dump())

    # 3. silences (on the proxy: same timeline, far smaller than the source)
    cached = await art.load_json("silences.json")
    if cached is not None:
        silences = [Silence(**item) for item in cached]
    else:
        silences = await detect_silences(proxy_path) if has_audio else []
        await art.save_json("silences.json", [asdict(s) for s in silences])

    # 4. scenes
    cached = await art.load_json("scenes.json")
    if cached is not None:
        scenes = [Scene(**item) for item in cached]
    else:
        scenes = await detect_scenes(proxy_path, silences=silences)
        await art.save_json("scenes.json", [asdict(s) for s in scenes])

    logger.info(
        "pre-analysis done job_id=%s words=%s silences=%s scenes=%s",
        job.id,
        len(transcript.all_words()),
        len(silences),
        len(scenes),
    )
    return PreAnalysisResult(proxy_path, transcript, silences, scenes, chunks)
