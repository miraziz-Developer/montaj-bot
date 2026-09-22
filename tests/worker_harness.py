"""Builds a fully faked WorkerDeps (real Postgres + real ffmpeg, fake Gemini/STT/Blob/Notifier)."""

import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import EditPlanRow, Job, UnitLedger, Upload, User
from app.models.enums import JobStatus, PlanSource, UploadStatus
from app.schemas.analysis import VideoAnalysis
from app.schemas.edit_plan import EditPlan
from app.services import billing
from app.services.media.probe import probe
from app.services.media.scenes import Scene
from app.services.stt.base import Transcript
from app.services.stt.fake import FakeSTTProvider
from app.worker.deps import WorkerDeps
from tests.factories import create_user
from tests.fakes.gemini import FakeGeminiClient, default_analysis, make_plan, make_transcript, speech
from tests.fakes.notifier import FakeNotifier
from tests.fakes.storage import FakeBlobStorage


def good_plan() -> EditPlan:
    return make_plan([(0.0, 2.5), (3.2, 5.5)])


def speech_transcript() -> Transcript:
    return make_transcript(speech(0.4, 5.6))


@dataclass
class Harness:
    session_factory: Callable[[], AsyncSession]
    deps: WorkerDeps
    storage: FakeBlobStorage
    gemini: FakeGeminiClient
    stt: FakeSTTProvider
    notifier: FakeNotifier
    enqueued: list[tuple[Any, ...]]
    settings: Settings
    clip: Path
    tmp_dir: Path
    _probe: Any = field(default=None)

    @property
    def ctx(self) -> dict[str, Any]:
        return {"deps": self.deps}

    # ---------- arrange ----------

    async def new_job(
        self,
        *,
        status: JobStatus = JobStatus.QUEUED,
        is_trial: bool = False,
        balance: int = 5,
        reserve: int = 1,
        revision_count: int = 0,
        current_plan_version: int = 0,
        referred_by: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> SimpleNamespace:
        info = self._probe or await probe(str(self.clip))
        self._probe = info
        async with self.session_factory() as session:
            if user_id is None:
                user = await create_user(
                    session,
                    balance_units=balance,
                    onboarding_completed=True,
                    niche="auto",
                    purpose="reels",
                    trial_used=is_trial,
                    phone_hash=f"phone-{uuid.uuid4()}",
                    referred_by=referred_by,
                )
                user_id = user.id
            upload = Upload(
                user_id=user_id,
                blob_path=f"{user_id}/{uuid.uuid4()}/source.mp4",
                original_filename="clip.mp4",
                content_type="video/mp4",
                size_bytes=self.clip.stat().st_size,
                status=UploadStatus.VERIFIED,
                duration_sec=info.duration_sec,
                width=info.width,
                height=info.height,
                fps=info.fps,
                has_audio=info.has_audio,
                video_codec=info.video_codec,
            )
            session.add(upload)
            await session.flush()
            job = Job(
                user_id=user_id,
                upload_id=upload.id,
                status=status,
                aspect="9:16",
                style_preset="dynamic_reels",
                units_cost=1,
                is_trial=is_trial,
                revision_count=revision_count,
                current_plan_version=current_plan_version,
            )
            session.add(job)
            await session.flush()
            if not is_trial and reserve:
                await billing.reserve_units(session, user_id, reserve, job.id)
            await session.commit()
            blob_path, job_id = upload.blob_path, job.id
        self.storage.put("uploads", blob_path, self.clip.read_bytes())
        return SimpleNamespace(user_id=user_id, job_id=job_id, blob_path=blob_path)

    async def add_plan(
        self,
        job_id: uuid.UUID,
        plan: EditPlan | None = None,
        version: int = 1,
        source: PlanSource = PlanSource.AI_INITIAL,
    ) -> EditPlan:
        plan = plan or good_plan()
        async with self.session_factory() as session:
            session.add(
                EditPlanRow(
                    job_id=job_id,
                    version=version,
                    plan_json=plan.model_dump(mode="json"),
                    human_summary=plan.human_summary_uz,
                    source=source,
                )
            )
            await session.execute(update(Job).where(Job.id == job_id).values(current_plan_version=version))
            await session.commit()
        return plan

    def seed_artifacts(
        self, job_id: uuid.UUID, *, transcript: Transcript | None = None, full: bool = True
    ) -> None:
        """What a finished analysis leaves in the `artifacts` container."""
        art = "artifacts"
        put = lambda name, data: self.storage.put(art, f"{job_id}/{name}", json.dumps(data).encode())  # noqa: E731
        put("transcript.json", (transcript or speech_transcript()).model_dump())
        if full:
            scenes = [Scene(1, 0.0, 3.0), Scene(2, 3.0, 6.0)]
            put("silences.json", [])
            put("scenes.json", [asdict(s) for s in scenes])
            put(
                "analysis.json",
                default_analysis([{"scene_id": s.scene_id} for s in scenes]).model_dump(mode="json"),
            )

    async def set_updated_at(self, job_id: uuid.UUID, when: datetime) -> None:
        async with self.session_factory() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(updated_at=when))
            await session.commit()

    async def set_status(self, job_id: uuid.UUID, status: JobStatus, **fields: Any) -> None:
        async with self.session_factory() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(status=status, **fields))
            await session.commit()

    # ---------- inspect ----------

    async def job(self, job_id: uuid.UUID) -> Job:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(Job).where(Job.id == job_id).execution_options(populate_existing=True)
                )
            ).scalar_one()

    async def user(self, user_id: uuid.UUID) -> User:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(User).where(User.id == user_id).execution_options(populate_existing=True)
                )
            ).scalar_one()

    async def plans(self, job_id: uuid.UUID) -> list[EditPlanRow]:
        async with self.session_factory() as session:
            rows = await session.execute(select(EditPlanRow).where(EditPlanRow.job_id == job_id))
            return sorted(rows.scalars().all(), key=lambda r: r.version)

    async def ledger(self, user_id: uuid.UUID) -> list[tuple[str, int]]:
        """(reason, delta) sorted by delta so the result never depends on clock ordering."""
        async with self.session_factory() as session:
            rows = await session.execute(
                select(UnitLedger.reason, UnitLedger.delta).where(UnitLedger.user_id == user_id)
            )
            return sorted(((str(r.value), d) for r, d in rows.all()), key=lambda row: row[1])


def build_harness(session_factory: Callable[[], AsyncSession], tmp_path: Path, clip: Path) -> Harness:
    assets = tmp_path / "assets"
    (assets / "fonts").mkdir(parents=True)
    (assets / "music").mkdir()
    (assets / "music" / "catalog.json").write_text("[]")
    settings = Settings(
        _env_file=None,
        tmp_dir=str(tmp_path / "tmp"),
        assets_dir=str(assets),
        gemini_analysis_model="analysis-model",
        gemini_planner_model="planner-model",
        bot_username="video_editor_uzbot",
        render_clip_concurrency=2,
    )
    storage, notifier = FakeBlobStorage(), FakeNotifier()
    gemini, stt = FakeGeminiClient(plan=good_plan()), FakeSTTProvider(default=speech_transcript())
    enqueued: list[tuple[Any, ...]] = []

    async def enqueue(function_name: str, *args: Any) -> None:
        enqueued.append((function_name, *args))

    deps = WorkerDeps(
        settings=settings,
        sessionmaker=session_factory,  # type: ignore[arg-type]
        gemini=gemini,
        stt=stt,
        storage=storage,
        notifier=notifier,
        enqueue=enqueue,
    )
    return Harness(
        session_factory, deps, storage, gemini, stt, notifier, enqueued, settings, clip, tmp_path / "tmp"
    )


__all__ = ["Harness", "VideoAnalysis", "build_harness", "good_plan", "speech_transcript"]
