import asyncio
import random
from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientUnits, NotFound, TrialUnavailable
from app.models import UnitLedger, User
from app.models.enums import LedgerReason
from app.services import billing
from tests.factories import create_job, create_user


async def _ledger_sum(session: AsyncSession, user_id) -> int:
    return await session.scalar(
        select(func.coalesce(func.sum(UnitLedger.delta), 0)).where(UnitLedger.user_id == user_id)
    )


async def test_grant_and_get_balance(session: AsyncSession) -> None:
    user = await create_user(session)
    assert await billing.grant_units(session, user.id, 5, LedgerReason.PURCHASE) == 5
    assert await billing.get_balance(session, user.id) == 5


async def test_grant_validates_input(session: AsyncSession) -> None:
    user = await create_user(session)
    with pytest.raises(ValueError):
        await billing.grant_units(session, user.id, 0, LedgerReason.PURCHASE)
    with pytest.raises(ValueError):
        await billing.grant_units(session, user.id, 1, LedgerReason.RESERVE)


async def test_unknown_user_raises_not_found(session: AsyncSession) -> None:
    import uuid

    with pytest.raises(NotFound):
        await billing.get_balance(session, uuid.uuid4())


async def test_reserve_insufficient_leaves_balance_unchanged(session: AsyncSession) -> None:
    user = await create_user(session, balance_units=2)
    job = await create_job(session, user.id)
    with pytest.raises(InsufficientUnits):
        await billing.reserve_units(session, user.id, 3, job.id)
    assert await billing.get_balance(session, user.id) == 2
    assert await session.scalar(select(func.count()).select_from(UnitLedger)) == 0


async def test_reserve_writes_negative_ledger_row(session: AsyncSession) -> None:
    user = await create_user(session, balance_units=5)
    job = await create_job(session, user.id)
    assert await billing.reserve_units(session, user.id, 2, job.id) == 3
    row = (await session.execute(select(UnitLedger))).scalar_one()
    assert (row.delta, row.reason, row.job_id) == (-2, LedgerReason.RESERVE, job.id)


async def test_ledger_invariant_after_random_mix(session: AsyncSession) -> None:
    rng = random.Random(1234)
    user = await create_user(session)
    jobs = [await create_job(session, user.id) for _ in range(15)]
    for _ in range(80):
        op = rng.choice(["grant", "reserve", "revision", "refund"])
        job = rng.choice(jobs)
        try:
            if op == "grant":
                await billing.grant_units(session, user.id, rng.randint(1, 5), LedgerReason.ADMIN_GRANT)
            elif op == "reserve":
                await billing.reserve_units(session, user.id, rng.randint(1, 4), job.id)
            elif op == "revision":
                await billing.charge_revision(session, user.id, job.id, 1)
            else:
                await billing.refund_units(session, job.id)
        except InsufficientUnits:
            pass
        balance = await billing.get_balance(session, user.id)
        assert balance >= 0
        assert balance == await _ledger_sum(session, user.id)


async def test_concurrent_reserve_only_one_succeeds(session_factory: Callable[[], AsyncSession]) -> None:
    async with session_factory() as setup:
        user = await create_user(setup, balance_units=1)
        jobs = [await create_job(setup, user.id) for _ in range(2)]
        await setup.commit()
        user_id, job_ids = user.id, [j.id for j in jobs]

    async def attempt(job_id) -> bool:
        async with session_factory() as s:
            try:
                await billing.reserve_units(s, user_id, 1, job_id)
                await s.commit()
                return True
            except InsufficientUnits:
                await s.rollback()
                return False

    results = await asyncio.gather(*(attempt(j) for j in job_ids))
    assert sorted(results) == [False, True]
    async with session_factory() as check:
        assert await billing.get_balance(check, user_id) == 0
        assert await check.scalar(select(func.count()).select_from(UnitLedger)) == 1


async def test_refund_is_idempotent(session: AsyncSession) -> None:
    user = await create_user(session, balance_units=5)
    job = await create_job(session, user.id)
    await billing.reserve_units(session, user.id, 3, job.id)
    assert await billing.refund_units(session, job.id) == 3
    assert await billing.refund_units(session, job.id) == 0
    assert await billing.get_balance(session, user.id) == 5
    refunds = await session.scalar(
        select(func.count()).select_from(UnitLedger).where(UnitLedger.reason == LedgerReason.REFUND)
    )
    assert refunds == 1


async def test_refund_includes_paid_revisions(session: AsyncSession) -> None:
    user = await create_user(session, balance_units=5)
    job = await create_job(session, user.id)
    await billing.reserve_units(session, user.id, 2, job.id)
    await billing.charge_revision(session, user.id, job.id, 1)
    assert await billing.refund_units(session, job.id) == 3
    assert await billing.get_balance(session, user.id) == 5


async def test_refund_without_spend_is_zero(session: AsyncSession) -> None:
    user = await create_user(session, balance_units=1)
    job = await create_job(session, user.id)
    assert await billing.refund_units(session, job.id) == 0


async def test_trial_used_once_then_released(session: AsyncSession) -> None:
    user = await create_user(session)
    await billing.use_trial(session, user.id)
    with pytest.raises(TrialUnavailable):
        await billing.use_trial(session, user.id)
    await billing.release_trial(session, user.id)
    await billing.use_trial(session, user.id)
    assert (await session.get(User, user.id)).trial_used is True
