import asyncio
from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidState, NotFound
from app.models import Payment, UnitLedger
from app.models.enums import LedgerReason, PaymentProvider, PaymentStatus
from app.services import payments
from app.services.tariffs import TARIFFS, get_tariff
from tests.factories import create_user


async def _pair(session: AsyncSession):  # noqa: ANN202
    return await create_user(session), await create_user(session, is_admin=True)


def test_tariffs_match_the_architecture_numbers() -> None:
    assert [(t.code, t.units, t.price_uzs, t.recommended) for t in TARIFFS] == [
        ("single", 1, 15_000, False),
        ("start", 10, 99_000, False),
        ("pro", 30, 249_000, True),
        ("max", 100, 690_000, False),
    ]


async def test_create_payment_request_uses_the_tariff_price(session: AsyncSession) -> None:
    user, _ = await _pair(session)
    payment = await payments.create_payment_request(session, user, "pro")
    assert (payment.plan_code, payment.amount_uzs, payment.status, payment.provider) == (
        "pro", 249_000, PaymentStatus.PENDING, PaymentProvider.MANUAL,
    )  # fmt: skip
    assert payment.user_id == user.id and payment.receipt_file_id is None


async def test_unknown_plan_code_is_rejected(session: AsyncSession) -> None:
    user, _ = await _pair(session)
    with pytest.raises(NotFound):
        await payments.create_payment_request(session, user, "diamond")
    assert await session.scalar(select(func.count()).select_from(Payment)) == 0


async def test_attach_receipt_only_while_pending(session: AsyncSession) -> None:
    user, admin = await _pair(session)
    payment = await payments.create_payment_request(session, user, "single")
    await payments.attach_receipt(session, payment, "file-1")
    assert payment.receipt_file_id == "file-1"
    await payments.approve_payment(session, payment, admin)
    with pytest.raises(InvalidState):
        await payments.attach_receipt(session, payment, "file-2")
    assert payment.receipt_file_id == "file-1"

    other = await payments.create_payment_request(session, user, "single")
    await payments.reject_payment(session, other, admin)
    with pytest.raises(InvalidState):
        await payments.attach_receipt(session, other, "file-3")


async def test_approve_grants_exactly_the_tariff_units(session: AsyncSession) -> None:
    user, admin = await _pair(session)
    payment = await payments.create_payment_request(session, user, "start")
    await payments.approve_payment(session, payment, admin)

    assert user.balance_units == get_tariff("start").units == 10
    assert (payment.status, payment.decided_by) == (PaymentStatus.APPROVED, admin.id)
    assert payment.decided_at is not None
    row = (await session.execute(select(UnitLedger))).scalar_one()
    assert (row.reason, row.delta, row.payment_id, row.user_id) == (
        LedgerReason.PURCHASE,
        10,
        payment.id,
        user.id,
    )


async def test_approving_twice_raises_and_never_grants_twice(session: AsyncSession) -> None:
    user, admin = await _pair(session)
    payment = await payments.create_payment_request(session, user, "pro")
    await payments.approve_payment(session, payment, admin)
    with pytest.raises(InvalidState):
        await payments.approve_payment(session, payment, admin)
    assert user.balance_units == 30
    assert await session.scalar(select(func.count()).select_from(UnitLedger)) == 1


async def test_reject_grants_nothing_and_cannot_be_followed_by_approve(session: AsyncSession) -> None:
    user, admin = await _pair(session)
    payment = await payments.create_payment_request(session, user, "max")
    await payments.reject_payment(session, payment, admin, note="blur")
    assert (payment.status, payment.note, payment.decided_by) == (PaymentStatus.REJECTED, "blur", admin.id)
    assert user.balance_units == 0
    with pytest.raises(InvalidState):
        await payments.approve_payment(session, payment, admin)
    assert await session.scalar(select(func.count()).select_from(UnitLedger)) == 0


async def test_the_owner_can_cancel_a_pending_payment(session: AsyncSession) -> None:
    user, admin = await _pair(session)
    payment = await payments.create_payment_request(session, user, "single")
    await payments.cancel_payment(session, payment, user)
    assert (payment.status, payment.note, payment.decided_by) == (
        PaymentStatus.REJECTED,
        "user_canceled",
        None,
    )
    with pytest.raises(InvalidState):
        await payments.cancel_payment(session, payment, user)
    other = await payments.create_payment_request(session, user, "single")
    with pytest.raises(NotFound):
        await payments.cancel_payment(session, other, admin)  # not the owner


async def test_concurrent_approvals_grant_once(session_factory: Callable[[], AsyncSession]) -> None:
    async with session_factory() as setup:
        user, admin = await _pair(setup)
        payment = await payments.create_payment_request(setup, user, "pro")
        await setup.commit()
        user_id, admin_id, payment_id = user.id, admin.id, payment.id

    async def approve() -> bool:
        async with session_factory() as s:
            admin_row = await s.get(type(admin), admin_id)
            pay = await s.get(Payment, payment_id)
            try:
                await payments.approve_payment(s, pay, admin_row)
                await s.commit()
                return True
            except InvalidState:
                await s.rollback()
                return False

    results = await asyncio.gather(approve(), approve(), approve())
    assert sorted(results) == [False, False, True]
    async with session_factory() as check:
        assert (await check.get(type(admin), user_id)).balance_units == 30
        assert await check.scalar(select(func.count()).select_from(UnitLedger)) == 1
