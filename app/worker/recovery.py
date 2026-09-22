"""Recovery of jobs stuck in a processing state (ARCHITECTURE section 16)."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import JobStatus
from app.models.job import Job
from app.services import jobs
from app.worker.failures import ErrorCode, apply_failure, friendly_uz_message_for
from app.worker.notifier import JobNotifier
from app.worker.queue import Enqueue

logger = logging.getLogger(__name__)

STUCK_AFTER = timedelta(minutes=30)
# stuck state -> (state to go back to, task to enqueue there or None)
_RETRY_PLAN: dict[JobStatus, tuple[JobStatus, str | None]] = {
    JobStatus.PREPROCESSING: (JobStatus.QUEUED, "run_analysis"),
    JobStatus.ANALYZING: (JobStatus.QUEUED, "run_analysis"),
    JobStatus.PLANNING: (JobStatus.QUEUED, "run_analysis"),
    # The revision text is not stored, so it cannot be replayed: the user simply asks again.
    JobStatus.REVISING: (JobStatus.AWAITING_PLAN_APPROVAL, None),
    # run_render reuses an already uploaded final.mp4, so a job stuck in DELIVERING only re-delivers.
    JobStatus.RENDERING: (JobStatus.AWAITING_PLAN_APPROVAL, "run_render"),
    JobStatus.DELIVERING: (JobStatus.AWAITING_PLAN_APPROVAL, "run_render"),
}


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    requeued: int = 0
    failed: int = 0


async def requeue_stuck_jobs(
    session: AsyncSession,
    enqueue: Enqueue,
    notifier: JobNotifier | None = None,
    *,
    now: datetime | None = None,
    older_than: timedelta = STUCK_AFTER,
) -> RecoveryReport:
    """First time stuck: step back and re-enqueue once (error_code=STUCK_RETRY). Stuck again: FAILED + refund.

    Owns its transactions: every job is committed BEFORE it is enqueued, so a worker never sees old state.
    """
    cutoff = (now or datetime.now(UTC)) - older_than
    rows = (
        await session.execute(
            select(Job.id, Job.status, Job.error_code).where(
                Job.status.in_(list(_RETRY_PLAN)), Job.updated_at < cutoff
            )
        )
    ).all()

    requeued = failed = 0
    for job_id, status, error_code in rows:
        if error_code == ErrorCode.STUCK_RETRY.value:
            job = await apply_failure(session, job_id, ErrorCode.STUCK_RETRY, "stuck twice")
            await session.commit()
            if job is not None:
                failed += 1
                if notifier is not None:
                    await _notify(notifier, job)
            continue
        back_state, task = _RETRY_PLAN[status]
        moved = await jobs.transition(
            session, job_id, [status], back_state, error_code=ErrorCode.STUCK_RETRY.value
        )
        await session.commit()
        if moved is None:
            continue
        requeued += 1
        logger.warning("stuck job job_id=%s was %s: back to %s", job_id, status, back_state)
        if task is not None:
            await enqueue(task, str(job_id))
    return RecoveryReport(requeued=requeued, failed=failed)


async def _notify(notifier: JobNotifier, job: Job) -> None:
    try:
        await notifier.failed(job, friendly_uz_message_for(ErrorCode.STUCK_RETRY))
    except Exception:
        logger.warning("could not notify about a stuck job job_id=%s", job.id, exc_info=True)
