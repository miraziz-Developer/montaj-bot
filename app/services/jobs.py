import logging
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidState
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.user import User
from app.services import billing

logger = logging.getLogger(__name__)

TERMINAL_STATES = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELED, JobStatus.EXPIRED)


async def transition(
    session: AsyncSession,
    job_id: uuid.UUID,
    from_states: Collection[JobStatus],
    to_state: JobStatus,
    **fields: Any,
) -> Job | None:
    """Atomically move a job (`UPDATE ... WHERE status = ANY(:from) RETURNING`).

    Returns the updated Job, or None (with a warning) when someone else moved it first: callers must return
    quietly in that case, never raise. `fields` are extra columns set in the same UPDATE.
    """
    stmt = (
        update(Job)
        .where(Job.id == job_id, Job.status.in_(list(from_states)))
        .values(status=to_state, updated_at=func.now(), **fields)
        .returning(Job)
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        logger.warning(
            "transition skipped job_id=%s: not in %s (target %s)",
            job_id,
            [s.value for s in from_states],
            to_state,
        )
    return job


async def count_active_jobs(session: AsyncSession, user_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(Job)
        .where(Job.user_id == user_id, Job.status.not_in(TERMINAL_STATES))
    )
    return int(await session.scalar(stmt) or 0)


CANCELABLE_STATES = (JobStatus.AWAITING_CONFIRM, JobStatus.QUEUED, JobStatus.AWAITING_PLAN_APPROVAL)


async def cancel_job(session: AsyncSession, job: Job, user: User) -> int:
    """Cancel on behalf of the owner (the ONE implementation, used by the API route and the bot handler).

    Refund policy (ARCHITECTURE section 7): canceled while QUEUED -> refund (and give a trial back);
    canceled while awaiting confirm/approval -> nothing to refund or the compute is already spent.
    Raises InvalidState. Runs in the caller's transaction (no commit). Returns the new balance.
    """
    previous = job.status
    if previous not in CANCELABLE_STATES:
        raise InvalidState()
    canceled = await transition(
        session, job.id, [previous], JobStatus.CANCELED, finished_at=datetime.now(UTC)
    )
    if canceled is None:
        raise InvalidState()
    if previous == JobStatus.QUEUED:
        await billing.refund_units(session, job.id)
        if job.is_trial:
            await billing.release_trial(session, user.id)
    balance = await billing.get_balance(session, user.id)
    logger.info("job canceled job_id=%s user_id=%s from=%s", job.id, user.id, previous)
    return balance
