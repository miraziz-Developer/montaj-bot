"""Load a job's saved artifacts (transcript, silences, scenes, analysis) back from Blob."""

from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select

from app.models.enums import SourceRole
from app.models.job import Job
from app.models.job_source import JobSource
from app.models.upload import Upload
from app.models.user import User
from app.schemas.analysis import VideoAnalysis
from app.services.ai.analysis import analyze_full_video
from app.services.ai.plan_text import BrollSourceInfo, PlanContext, SourceInfo
from app.services.ai.plan_validator import DEFAULT_WATERMARK_TEXT
from app.services.media.pipeline import ArtifactStore
from app.services.media.proxy import make_proxy
from app.services.media.scenes import Scene, detect_scenes
from app.services.media.silence import Silence
from app.services.render.music import load_music_catalog
from app.services.stt.base import Transcript
from app.worker.deps import WorkerDeps


class ArtifactMissing(RuntimeError):
    """A required artifact is not in Blob (expired after 48 h, or never produced)."""


def artifact_store(deps: WorkerDeps, job: Job, workdir: Path) -> ArtifactStore:
    workdir.mkdir(parents=True, exist_ok=True)
    return ArtifactStore(deps.storage, deps.settings.azure_artifacts_container, job.id, workdir)


async def _require(store: ArtifactStore, name: str) -> object:
    data = await store.load_json(name)
    if data is None:
        raise ArtifactMissing(f"artifact {name} is missing")
    return data


async def load_transcript(store: ArtifactStore) -> Transcript:
    return Transcript.model_validate(await _require(store, "transcript.json"))


def watermark_text(deps: WorkerDeps) -> str:
    username = deps.settings.bot_username
    return f"@{username}" if username else DEFAULT_WATERMARK_TEXT


async def broll_rows(deps: WorkerDeps, job_id: object) -> list[tuple[JobSource, Upload]]:
    """P13: every attached B-roll upload, in attach order. Empty for the overwhelming majority of jobs."""
    async with deps.sessionmaker() as session:
        rows = (
            await session.execute(
                select(JobSource, Upload)
                .join(Upload, Upload.id == JobSource.upload_id)
                .where(JobSource.job_id == job_id, JobSource.role == SourceRole.BROLL)
                .order_by(JobSource.position)
            )
        ).all()
    return [(js, up) for js, up in rows]


async def download_broll(deps: WorkerDeps, job_id: object, workdir: Path) -> dict[str, Path]:
    """`{source_id: local_path}` for every attached B-roll upload (P13), downloaded next to the primary."""
    paths: dict[str, Path] = {}
    for i, (_row, upload) in enumerate(await broll_rows(deps, job_id), start=1):
        local = workdir / f"broll_{i}{Path(upload.blob_path).suffix}"
        await deps.storage.download_to_file(deps.settings.azure_uploads_container, upload.blob_path, local)
        paths[f"broll_{i}"] = local
    return paths


async def broll_sources_for_planning(
    deps: WorkerDeps, job: Job, workdir: Path, store: ArtifactStore
) -> list[BrollSourceInfo]:
    """P13: for each attached B-roll upload, a proxy + scene detection + a cheap frame-only analysis (no
    STT - B-roll is muted cutaway footage, so there is no speech to transcribe). Cached in `store` so a
    revision does not re-pay for the analysis of unchanged B-roll footage."""
    cached = await store.load_json("broll_sources.json")
    if cached is not None:
        return [BrollSourceInfo(**item) for item in cached]

    infos: list[BrollSourceInfo] = []
    empty_transcript = Transcript(language="", segments=[])
    for i, (_row, upload) in enumerate(await broll_rows(deps, job.id), start=1):
        source_id = f"broll_{i}"
        local = workdir / f"{source_id}{Path(upload.blob_path).suffix}"
        await deps.storage.download_to_file(deps.settings.azure_uploads_container, upload.blob_path, local)
        proxy = workdir / f"{source_id}_proxy.mp4"
        await make_proxy(local, proxy, short_side=720)
        scenes = await detect_scenes(proxy)
        analysis, _usage = await analyze_full_video(
            deps.gemini,
            proxy_path=proxy,
            scenes=scenes,
            transcript=empty_transcript,
            niche="",
            purpose="",
            chunk_sec=deps.settings.analysis_chunk_sec,
            max_concurrency=deps.settings.llm_max_concurrency,
            fps=deps.settings.gemini_analysis_fps,
        )
        by_id = {a.scene_id: a for a in analysis.scenes}
        scene_dicts = [
            {
                "scene_id": s.scene_id,
                "start": round(s.start, 2),
                "end": round(s.end, 2),
                "description": by_id[s.scene_id].description if s.scene_id in by_id else "",
            }
            for s in scenes
        ]
        infos.append(
            BrollSourceInfo(
                source_id=source_id,
                duration_sec=float(upload.duration_sec or 0),
                width=int(upload.width or 0),
                height=int(upload.height or 0),
                scenes=scene_dicts,
            )
        )
    await store.save_json("broll_sources.json", [asdict(i) for i in infos])
    return infos


async def load_plan_context(
    deps: WorkerDeps, job: Job, upload: Upload, user: User, workdir: Path
) -> PlanContext:
    """Rebuild the planning context of a finished analysis (used by revisions)."""
    store = artifact_store(deps, job, workdir)
    transcript = await load_transcript(store)
    return PlanContext(
        source=SourceInfo(
            duration_sec=float(upload.duration_sec or 0),
            width=int(upload.width or 0),
            height=int(upload.height or 0),
            has_audio=bool(upload.has_audio),
            has_speech=bool(transcript.all_words()),
        ),
        analysis=VideoAnalysis.model_validate(await _require(store, "analysis.json")),
        scenes=[Scene(**item) for item in await _require(store, "scenes.json")],  # type: ignore[attr-defined]
        transcript=transcript,
        silences=[Silence(**item) for item in await _require(store, "silences.json")],  # type: ignore[attr-defined]
        music_tracks=load_music_catalog(deps.settings.assets_dir),
        creator_profile={"niche": user.niche or "", "purpose": user.purpose or ""},
        broll_sources=await broll_sources_for_planning(deps, job, workdir, store),
    )
