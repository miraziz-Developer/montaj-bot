"""Manual payments (MVP): pending -> approved/rejected by an admin. Units are granted exactly once."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidState, NotFound
from app.models.enums import LedgerReason, PaymentProvider, PaymentStatus
from app.models.payment import Payment
from app.models.user import User
from app.services import billing
from app.services.tariffs import get_tariff

logger = logging.getLogger(__name__)


async def _lock(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    """Re-read the payment under a row lock so concurrent decisions cannot both pass the status check."""
    stmt = (
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    payment = (await session.execute(stmt)).scalar_one_or_none()
    if payment is None:
        raise NotFound()
    return payment


def _require_pending(payment: Payment) -> None:
    if payment.status != PaymentStatus.PENDING:
        raise InvalidState()


async def create_payment_request(session: AsyncSession, user: User, plan_code: str) -> Payment:
    """A pending payment for a known tariff (NotFound for an unknown plan_code)."""
    tariff = get_tariff(plan_code)
    payment = Payment(
        user_id=user.id,
        plan_code=tariff.code,
        amount_uzs=tariff.price_uzs,
        provider=PaymentProvider.MANUAL,
        status=PaymentStatus.PENDING,
    )
    session.add(payment)
    await session.flush()
    return payment


async def attach_receipt(session: AsyncSession, payment: Payment, telegram_file_id: str) -> Payment:
    payment = await _lock(session, payment.id)
    _require_pending(payment)
    payment.receipt_file_id = telegram_file_id
    await session.flush()
    return payment


async def approve_payment(session: AsyncSession, payment: Payment, admin: User) -> Payment:
    """Grant the tariff's units and mark the payment approved in ONE transaction (2nd call: InvalidState)."""
    payment = await _lock(session, payment.id)
    _require_pending(payment)
    tariff = get_tariff(payment.plan_code)
    await billing.grant_units(
        session,
        payment.user_id,
        tariff.units,
        LedgerReason.PURCHASE,
        payment_id=payment.id,
        note=f"tariff:{tariff.code}",
    )
    payment.status = PaymentStatus.APPROVED
    payment.decided_by = admin.id
    payment.decided_at = datetime.now(UTC)
    await session.flush()
    logger.info(
        "payment approved payment_id=%s admin=%s units=%s", payment.id, admin.telegram_id, tariff.units
    )
    return payment


async def reject_payment(
    session: AsyncSession, payment: Payment, admin: User, note: str | None = None
) -> Payment:
    payment = await _lock(session, payment.id)
    _require_pending(payment)
    payment.status = PaymentStatus.REJECTED
    payment.decided_by = admin.id
    payment.decided_at = datetime.now(UTC)
    payment.note = note
    await session.flush()
    logger.info("payment rejected payment_id=%s admin=%s", payment.id, admin.telegram_id)
    return payment


async def cancel_payment(session: AsyncSession, payment: Payment, user: User) -> Payment:
    """The paying user withdraws a pending request (recorded as rejected with note `user_canceled`)."""
    payment = await _lock(session, payment.id)
    if payment.user_id != user.id:
        raise NotFound()
    _require_pending(payment)
    payment.status = PaymentStatus.REJECTED
    payment.decided_at = datetime.now(UTC)
    payment.note = "user_canceled"
    await session.flush()
    return payment
