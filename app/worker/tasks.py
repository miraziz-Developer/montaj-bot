"""arq tasks: the job pipeline. Any exception becomes FAILED + refund + user message (no arq retry)."""

import asyncio
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.bot import texts
from app.core.errors import InsufficientUnits
from app.models.enums import JobStatus, PlanSource
from app.models.job import Job
from app.models.plan import EditPlanRow
from app.models.upload import Upload
from app.models.user import User
from app.schemas.analysis import VideoAnalysis
from app.schemas.edit_plan import EditPlan
from app.services import billing, jobs
from app.services.ai.analysis import analyze_full_video
from app.services.ai.llm import UsageInfo
from app.services.ai.plan_text import PlanContext, SourceInfo
from app.services.ai.planner import build_initial_plan, build_revised_plan
from app.services.delivery import deliver_result
from app.services.media.pipeline import PreAnalysisResult, run_pre_analysis
from app.services.render.engine import render_plan
from app.services.render.music import load_music_catalog
from app.worker.artifacts import (
    artifact_store,
    load_plan_context,
    load_transcript,
    watermark_text,
)
from app.worker.costs import estimate_cost_usd
from app.worker.deps import WorkerDeps
from app.worker.failures import ErrorCode, Stage, classify_error, fail_job
from app.worker.recovery import requeue_stuck_jobs

logger = logging.getLogger(__name__)

BLOB_RETENTION = timedelta(hours=48)
TMP_RETENTION = timedelta(hours=6)
STALE_PLAN_AFTER = timedelta(hours=24)
LIGHT_MODE_SHORT_SIDE = 720


# ---------- small helpers ----------


def _deps(ctx: dict[str, Any]) -> WorkerDeps:
    return ctx["deps"]


async def _safe(awaitable: Awaitable[Any]) -> None:
    """Notifications must never break the pipeline."""
    try:
        await awaitable
    except Exception:
        logger.warning("notification failed", exc_info=True)


async def _transition(
    deps: WorkerDeps, job_id: uuid.UUID, from_states: list[JobStatus], to_state: JobStatus, **fields: Any
) -> Job | None:
    async with deps.sessionmaker() as session:
        job = await jobs.transition(session, job_id, from_states, to_state, **fields)
        await session.commit()
        return job


async def _update(deps: WorkerDeps, job_id: uuid.UUID, **fields: Any) -> None:
    async with deps.sessionmaker() as session:
        await session.execute(update(Job).where(Job.id == job_id).values(**fields))
        await session.commit()


async def _load(deps: WorkerDeps, job: Job) -> tuple[Upload, User]:
    async with deps.sessionmaker() as session:
        upload = await session.get(Upload, job.upload_id)
        user = await session.get(User, job.user_id)
    assert upload is not None and user is not None
    return upload, user


async def _plan_row(deps: WorkerDeps, job_id: uuid.UUID, version: int) -> EditPlanRow:
    async with deps.sessionmaker() as session:
        row = (
            await session.execute(
                select(EditPlanRow).where(EditPlanRow.job_id == job_id, EditPlanRow.version == version)
            )
        ).scalar_one()
    return row


def _workdir(deps: WorkerDeps, name: str) -> Path:
    return Path(deps.settings.tmp_dir) / name


def _tokens(job: Job, *usages: UsageInfo | None) -> dict[str, int]:
    used = [u for u in usages if u is not None]
    return {
        "llm_input_tokens": job.llm_input_tokens + sum(u.input_tokens for u in used),
        "llm_output_tokens": job.llm_output_tokens + sum(u.output_tokens for u in used),
    }


def _cost(deps: WorkerDeps, job: Job, tokens: dict[str, int], **overrides: Any) -> Any:
    return estimate_cost_usd(
        llm_input_tokens=tokens["llm_input_tokens"],
        llm_output_tokens=tokens["llm_output_tokens"],
        stt_seconds=overrides.get("stt_seconds", job.stt_seconds),
        render_seconds=overrides.get("render_seconds", job.render_seconds),
        settings=deps.settings,
    )


# ---------- analysis ----------


async def _analysis(
    deps: WorkerDeps, job: Job, upload: Upload, user: User, pre: PreAnalysisResult, workdir: Path
) -> tuple[VideoAnalysis, UsageInfo]:
    store = artifact_store(deps, job, workdir)
    cached = await store.load_json("analysis.json")
    if cached is not None:  # idempotent: a re-run does not pay for the analysis twice
        return VideoAnalysis.model_validate(cached), UsageInfo()
    settings = deps.settings
    long_video = float(upload.duration_sec or 0) > settings.long_video_threshold_sec
    analysis, usage = await analyze_full_video(
        deps.gemini,
        proxy_path=pre.proxy_path,
        scenes=pre.scenes,
        transcript=pre.transcript,
        niche=user.niche or "",
        purpose=user.purpose or "",
        chunk_sec=settings.analysis_chunk_sec,
        max_concurrency=settings.llm_max_concurrency,
        fps=settings.gemini_analysis_fps_long if long_video else settings.gemini_analysis_fps,
    )
    await store.save_json("analysis.json", analysis.model_dump(mode="json"))
    return analysis, usage


async def run_analysis(ctx: dict[str, Any], job_id: str) -> None:
    """QUEUED -> PREPROCESSING -> ANALYZING -> PLANNING -> AWAITING_PLAN_APPROVAL."""
    deps, jid = _deps(ctx), uuid.UUID(job_id)
    job = await _transition(deps, jid, [JobStatus.QUEUED], JobStatus.PREPROCESSING)
    if job is None:  # already being handled
        return
    workdir, stage = _workdir(deps, job_id), Stage.PREPARE
    try:
        upload, user = await _load(deps, job)
        await _safe(deps.notifier.progress(job, texts.PROGRESS_PREPARING))
        pre = await run_pre_analysis(job, upload, workdir, deps.storage, deps.stt, deps.settings)
        stt_seconds = sum(c.end_sec - c.start_sec for c in pre.chunks) if pre.chunks else job.stt_seconds
        await _update(deps, jid, stt_seconds=stt_seconds)

        stage = Stage.ANALYZE
        if await _transition(deps, jid, [JobStatus.PREPROCESSING], JobStatus.ANALYZING) is None:
            return
        await _safe(deps.notifier.progress(job, texts.PROGRESS_ANALYZING))
        analysis, analysis_usage = await _analysis(deps, job, upload, user, pre, workdir)

        stage = Stage.PLAN
        if await _transition(deps, jid, [JobStatus.ANALYZING], JobStatus.PLANNING) is None:
            return
        await _safe(deps.notifier.progress(job, texts.PROGRESS_PLANNING))
        plan_ctx = PlanContext(
            source=SourceInfo(
                duration_sec=float(upload.duration_sec or 0),
                width=int(upload.width or 0),
                height=int(upload.height or 0),
                has_audio=bool(upload.has_audio),
                has_speech=bool(pre.transcript.all_words()),
            ),
            analysis=analysis,
            scenes=pre.scenes,
            transcript=pre.transcript,
            silences=pre.silences,
            music_tracks=load_music_catalog(deps.settings.assets_dir),
            creator_profile={"niche": user.niche or "", "purpose": user.purpose or ""},
        )
        plan, source, plan_usage = await build_initial_plan(
            deps.gemini, job=job, ctx=plan_ctx, watermark_text=watermark_text(deps)
        )
        await artifact_store(deps, job, workdir).save_json("plan_v1.json", plan.model_dump(mode="json"))

        tokens = _tokens(job, analysis_usage, plan_usage)
        async with deps.sessionmaker() as session:
            session.add(
                EditPlanRow(
                    job_id=jid,
                    version=1,
                    plan_json=plan.model_dump(mode="json"),
                    human_summary=plan.human_summary_uz,
                    source=source,
                )
            )
            ready = await jobs.transition(
                session,
                jid,
                [JobStatus.PLANNING],
                JobStatus.AWAITING_PLAN_APPROVAL,
                current_plan_version=1,
                error_code=None,
                est_cost_usd=_cost(deps, job, tokens, stt_seconds=stt_seconds),
                **tokens,
            )
            if ready is None:
                await session.rollback()
                return
            await session.commit()
        await _safe(deps.notifier.plan_ready(ready, plan))
    except asyncio.CancelledError:
        await asyncio.shield(fail_job(deps, jid, ErrorCode.INTERNAL_ERROR, "cancelled (job timeout?)"))
        raise
    except Exception as exc:
        await fail_job(deps, jid, classify_error(exc, stage), exc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- revision ----------


async def run_revision(ctx: dict[str, Any], job_id: str, message: str) -> None:
    """AWAITING_PLAN_APPROVAL -> REVISING -> AWAITING_PLAN_APPROVAL. A bad revision never fails the job."""
    deps, jid = _deps(ctx), uuid.UUID(job_id)
    job = await _transition(deps, jid, [JobStatus.AWAITING_PLAN_APPROVAL], JobStatus.REVISING)
    if job is None:  # e.g. the user double-clicked
        return
    settings, workdir = deps.settings, _workdir(deps, f"{job_id}-rev")

    async def give_back(text_uz: str, **fields: Any) -> None:
        await _transition(deps, jid, [JobStatus.REVISING], JobStatus.AWAITING_PLAN_APPROVAL, **fields)
        await _safe(deps.notifier.progress(job, text_uz))

    try:
        await _safe(deps.notifier.progress(job, texts.PROGRESS_REVISING))
        upload, user = await _load(deps, job)
        cost = settings.revision_cost_units
        paid = job.revision_count >= settings.free_revisions_per_job
        if paid:
            async with deps.sessionmaker() as session:
                balance = await billing.get_balance(session, job.user_id)
            if balance < cost:
                await give_back(texts.FREE_EDITS_EXHAUSTED)
                return

        current = EditPlan.model_validate((await _plan_row(deps, jid, job.current_plan_version)).plan_json)
        plan_ctx = await load_plan_context(deps, job, upload, user, workdir)
        plan, changes, unsupported, source, usage = await build_revised_plan(
            deps.gemini,
            job=job,
            current_plan=current,
            message=message,
            ctx=plan_ctx,
            watermark_text=watermark_text(deps),
        )
        tokens = _tokens(job, usage)
        if plan == current:  # the LLM failed or changed nothing: no new version, nothing charged
            await give_back(texts.REVISION_NOT_APPLIED, **tokens, est_cost_usd=_cost(deps, job, tokens))
            return

        version = job.current_plan_version + 1
        async with deps.sessionmaker() as session:
            try:
                if paid:
                    await billing.charge_revision(session, job.user_id, jid, cost)
            except InsufficientUnits:  # the balance dropped while the LLM was working
                await session.rollback()
                await give_back(texts.FREE_EDITS_EXHAUSTED)
                return
            session.add(
                EditPlanRow(
                    job_id=jid,
                    version=version,
                    plan_json=plan.model_dump(mode="json"),
                    human_summary=plan.human_summary_uz,
                    source=PlanSource.AI_REVISION if source is None else source,
                    user_feedback=message[:2000],
                )
            )
            ready = await jobs.transition(
                session,
                jid,
                [JobStatus.REVISING],
                JobStatus.AWAITING_PLAN_APPROVAL,
                revision_count=job.revision_count + 1,
                current_plan_version=version,
                est_cost_usd=_cost(deps, job, tokens),
                **tokens,
            )
            if ready is None:
                await session.rollback()
                return
            await session.commit()
        await _safe(deps.notifier.plan_ready(ready, plan, changes, unsupported))
    except Exception:
        logger.exception("revision failed job_id=%s", job_id)
        await give_back(texts.REVISION_NOT_APPLIED)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- render ----------


async def run_render(ctx: dict[str, Any], job_id: str) -> None:
    """AWAITING_PLAN_APPROVAL -> RENDERING -> DELIVERING -> DONE (rendered from the ORIGINAL upload)."""
    deps, jid = _deps(ctx), uuid.UUID(job_id)
    job = await _transition(deps, jid, [JobStatus.AWAITING_PLAN_APPROVAL], JobStatus.RENDERING)
    if job is None:
        return
    settings, workdir, stage = deps.settings, _workdir(deps, job_id), Stage.RENDER
    try:
        await _safe(deps.notifier.progress(job, texts.PROGRESS_RENDERING))
        upload, _ = await _load(deps, job)
        outputs, blob = settings.azure_outputs_container, f"{job_id}/final.mp4"
        render_seconds = job.render_seconds
        size = await deps.storage.get_blob_size(outputs, blob)
        if size is None:  # idempotent: a recovered job re-delivers instead of re-rendering
            plan = EditPlan.model_validate((await _plan_row(deps, jid, job.current_plan_version)).plan_json)
            light = float(upload.duration_sec or 0) > settings.long_video_threshold_sec
            plan = plan.model_copy(
                update={
                    "export": plan.export.model_copy(
                        update={
                            "preset": "ultrafast" if light else settings.render_preset,
                            "crf": settings.render_crf,
                        }
                    )
                }
            )
            transcript = await load_transcript(artifact_store(deps, job, workdir))
            source = workdir / f"source{Path(upload.blob_path).suffix}"
            await deps.storage.download_to_file(settings.azure_uploads_container, upload.blob_path, source)
            final = workdir / "final.mp4"
            stats = await render_plan(
                source=source,
                plan=plan,
                transcript=transcript,
                workdir=workdir,
                out_path=final,
                assets_dir=Path(settings.assets_dir),
                max_short_side=LIGHT_MODE_SHORT_SIDE if light else 1080,
                clip_concurrency=settings.render_clip_concurrency,
            )
            await deps.storage.upload_file(outputs, blob, final, "video/mp4")
            render_seconds += stats.render_seconds
            size = stats.size_bytes

        tokens = _tokens(job)
        delivering = await _transition(
            deps,
            jid,
            [JobStatus.RENDERING],
            JobStatus.DELIVERING,
            output_blob_path=blob,
            output_size_bytes=size,
            render_seconds=render_seconds,
            est_cost_usd=_cost(deps, job, tokens, render_seconds=render_seconds),
        )
        if delivering is None:
            return
        stage = Stage.DELIVER
        await deliver_result(deps, delivering)  # sends the file (or a 48 h link), then DONE + referral reward
    except asyncio.CancelledError:
        await asyncio.shield(fail_job(deps, jid, ErrorCode.INTERNAL_ERROR, "cancelled (job timeout?)"))
        raise
    except Exception as exc:
        await fail_job(deps, jid, classify_error(exc, stage), exc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------- housekeeping ----------


def _remove_old_tmp_dirs(root: Path, older_than: timedelta) -> int:
    if not root.is_dir():
        return 0
    cutoff = time.time() - older_than.total_seconds()
    removed = 0
    for child in root.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


async def _expire_stale_plans(deps: WorkerDeps) -> int:
    """AWAITING_PLAN_APPROVAL with no action for 24 h -> EXPIRED (no refund: the compute is spent)."""
    cutoff = datetime.now(UTC) - STALE_PLAN_AFTER
    async with deps.sessionmaker() as session:
        ids = (
            (
                await session.execute(
                    select(Job.id).where(
                        Job.status == JobStatus.AWAITING_PLAN_APPROVAL, Job.updated_at < cutoff
                    )
                )
            )
            .scalars()
            .all()
        )
        expired = 0
        for job_id in ids:
            if await jobs.transition(
                session,
                job_id,
                [JobStatus.AWAITING_PLAN_APPROVAL],
                JobStatus.EXPIRED,
                finished_at=datetime.now(UTC),
            ):
                expired += 1
        await session.commit()
    return expired


async def cleanup_expired(ctx: dict[str, Any]) -> dict[str, int]:
    """Safety net beside the Blob lifecycle rule: blobs older than 48 h, stale temp dirs, stale plans."""
    deps, settings = _deps(ctx), _deps(ctx).settings
    blobs = 0
    for container in (
        settings.azure_uploads_container,
        settings.azure_artifacts_container,
        settings.azure_outputs_container,
    ):
        for name in await deps.storage.list_old_blobs(container, BLOB_RETENTION):
            await deps.storage.delete_blob(container, name)
            blobs += 1
    tmp_dirs = await asyncio.to_thread(_remove_old_tmp_dirs, Path(settings.tmp_dir), TMP_RETENTION)
    expired = await _expire_stale_plans(deps)
    logger.info("cleanup done blobs=%s tmp_dirs=%s expired_plans=%s", blobs, tmp_dirs, expired)
    return {"blobs": blobs, "tmp_dirs": tmp_dirs, "expired_plans": expired}


async def requeue_stuck(ctx: dict[str, Any]) -> None:
    """Cron wrapper around `requeue_stuck_jobs`."""
    deps = _deps(ctx)
    async with deps.sessionmaker() as session:
        report = await requeue_stuck_jobs(session, deps.enqueue, deps.notifier)
    if report.requeued or report.failed:
        logger.warning("stuck-job recovery requeued=%s failed=%s", report.requeued, report.failed)
