import asyncio
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job
from app.models.enums import JobStatus
from app.services.jobs import count_active_jobs, transition
from tests.factories import create_job, create_user


async def test_transition_moves_once(session: AsyncSession) -> None:
    user = await create_user(session)
    job = await create_job(session, user.id, status=JobStatus.AWAITING_CONFIRM)
    assert await transition(session, job.id, [JobStatus.AWAITING_CONFIRM], JobStatus.QUEUED, aspect="9:16")
    assert not await transition(session, job.id, [JobStatus.AWAITING_CONFIRM], JobStatus.QUEUED)
    await session.refresh(job)
    assert (job.status, job.aspect) == (JobStatus.QUEUED, "9:16")


async def test_transition_accepts_several_source_states(session: AsyncSession) -> None:
    user = await create_user(session)
    job = await create_job(session, user.id, status=JobStatus.QUEUED)
    assert await transition(
        session, job.id, [JobStatus.AWAITING_CONFIRM, JobStatus.QUEUED], JobStatus.CANCELED
    )


async def test_concurrent_transition_has_one_winner(session_factory: Callable[[], AsyncSession]) -> None:
    async with session_factory() as setup:
        user = await create_user(setup)
        job = await create_job(setup, user.id, status=JobStatus.AWAITING_CONFIRM)
        await setup.commit()
        job_id = job.id

    async def attempt() -> bool:
        async with session_factory() as s:
            moved = await transition(s, job_id, [JobStatus.AWAITING_CONFIRM], JobStatus.QUEUED)
            await s.commit()
            return moved is not None

    results = await asyncio.gather(*(attempt() for _ in range(4)))
    assert sorted(results) == [False, False, False, True]
    async with session_factory() as check:
        assert (await check.get(Job, job_id)).status == JobStatus.QUEUED


async def test_count_active_jobs_ignores_terminal(session: AsyncSession) -> None:
    user = await create_user(session)
    for status in (
        JobStatus.AWAITING_CONFIRM,
        JobStatus.RENDERING,
        JobStatus.DONE,
        JobStatus.FAILED,
        JobStatus.CANCELED,
        JobStatus.EXPIRED,
    ):
        await create_job(session, user.id, status=status)
    assert await count_active_jobs(session, user.id) == 2
