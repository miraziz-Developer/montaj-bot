import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, get_enqueue
from app.core.db import get_session
from app.core.errors import InvalidState, NotFound, TrialUnavailable
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.user import User
from app.schemas.api import ConfirmIn, ConfirmOut, JobListOut, JobPublic
from app.services import billing, jobs
from app.worker.queue import Enqueue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


async def _get_owned_job(session: AsyncSession, user: User, job_id: uuid.UUID) -> Job:
    stmt = (
        select(Job).where(Job.id == job_id, Job.user_id == user.id).execution_options(populate_existing=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise NotFound()
    return job


@router.get("", response_model=JobListOut)
async def list_jobs(
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JobListOut:
    stmt = select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return JobListOut(jobs=[JobPublic.model_validate(row) for row in rows])


@router.get("/{job_id}", response_model=JobPublic)
async def get_job(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JobPublic:
    return JobPublic.model_validate(await _get_owned_job(session, user, job_id))


@router.post("/{job_id}/confirm", response_model=ConfirmOut)
async def confirm_job(
    job_id: uuid.UUID,
    body: ConfirmIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    enqueue: Enqueue = Depends(get_enqueue),
) -> ConfirmOut:
    job = await _get_owned_job(session, user, job_id)
    # One transaction: atomic state change + trial/units reservation. Any error below rolls all of it back.
    moved = await jobs.transition(
        session,
        job.id,
        [JobStatus.AWAITING_CONFIRM],
        JobStatus.QUEUED,
        aspect=body.aspect,
        style_preset=body.style_preset,
        brief=body.brief or None,
        chat_id=user.telegram_id,  # private chat: the chat id equals the user id
    )
    if not moved:
        raise InvalidState()
    if job.is_trial:
        if not billing.trial_available(user):
            raise TrialUnavailable()
        await billing.use_trial(session, user.id)
        balance = user.balance_units
    else:
        balance = await billing.reserve_units(session, user.id, job.units_cost, job.id)
    await session.commit()

    try:
        await enqueue("run_analysis", str(job.id))
    except Exception:
        # ASSUMPTION: the job stays QUEUED; worker startup recovery (P09) re-enqueues stuck jobs.
        logger.exception("enqueue failed job_id=%s", job.id)
    return ConfirmOut(job_id=job.id, status=JobStatus.QUEUED, balance_units=balance)


@router.post("/{job_id}/cancel", response_model=ConfirmOut)
async def cancel_job(
    job_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ConfirmOut:
    job = await _get_owned_job(session, user, job_id)
    balance = await jobs.cancel_job(session, job, user)
    await session.commit()
    return ConfirmOut(job_id=job.id, status=JobStatus.CANCELED, balance_units=balance)
