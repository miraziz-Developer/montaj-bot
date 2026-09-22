"""Load a job's saved artifacts (transcript, silences, scenes, analysis) back from Blob."""

from pathlib import Path

from app.models.job import Job
from app.models.upload import Upload
from app.models.user import User
from app.schemas.analysis import VideoAnalysis
from app.services.ai.plan_text import PlanContext, SourceInfo
from app.services.ai.plan_validator import DEFAULT_WATERMARK_TEXT
from app.services.media.pipeline import ArtifactStore
from app.services.media.scenes import Scene
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
    )
