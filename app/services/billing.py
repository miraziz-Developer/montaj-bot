"""Units ledger operations.

Every function runs inside the CALLER's transaction (it never commits) and locks the user row with
SELECT ... FOR UPDATE, so balance changes and ledger rows are always written together.
"""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientUnits, NotFound, TrialUnavailable
from app.models.enums import LedgerReason
from app.models.ledger import UnitLedger
from app.models.user import User

logger = logging.getLogger(__name__)

_GRANT_REASONS = {
    LedgerReason.PURCHASE,
    LedgerReason.REFERRAL,
    LedgerReason.ADMIN_GRANT,
    LedgerReason.REFUND,
}
_SPEND_REASONS = (LedgerReason.RESERVE, LedgerReason.REVISION)


async def _lock_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    stmt = select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise NotFound()
    return user


async def _apply(
    session: AsyncSession,
    user: User,
    delta: int,
    reason: LedgerReason,
    *,
    job_id: uuid.UUID | None = None,
    payment_id: uuid.UUID | None = None,
    note: str | None = None,
) -> None:
    user.balance_units += delta
    session.add(
        UnitLedger(
            user_id=user.id, delta=delta, reason=reason, job_id=job_id, payment_id=payment_id, note=note
        )
    )
    await session.flush()


async def get_balance(session: AsyncSession, user_id: uuid.UUID) -> int:
    balance = await session.scalar(select(User.balance_units).where(User.id == user_id))
    if balance is None:
        raise NotFound()
    return balance


async def grant_units(
    session: AsyncSession,
    user_id: uuid.UUID,
    units: int,
    reason: LedgerReason,
    *,
    payment_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    note: str | None = None,
) -> int:
    """Add `units` (> 0) to the balance. Returns the new balance."""
    if units <= 0:
        raise ValueError("units must be positive")
    if reason not in _GRANT_REASONS:
        raise ValueError(f"reason {reason} cannot be used to grant units")
    user = await _lock_user(session, user_id)
    await _apply(session, user, units, reason, job_id=job_id, payment_id=payment_id, note=note)
    logger.info("granted units user_id=%s units=%s reason=%s", user_id, units, reason)
    return user.balance_units


async def _spend(
    session: AsyncSession, user_id: uuid.UUID, units: int, job_id: uuid.UUID, reason: LedgerReason
) -> int:
    if units <= 0:
        raise ValueError("units must be positive")
    user = await _lock_user(session, user_id)
    if user.balance_units < units:
        raise InsufficientUnits()
    await _apply(session, user, -units, reason, job_id=job_id)
    logger.info("spent units user_id=%s job_id=%s units=%s reason=%s", user_id, job_id, units, reason)
    return user.balance_units


async def reserve_units(session: AsyncSession, user_id: uuid.UUID, units: int, job_id: uuid.UUID) -> int:
    """Reserve units for a job (negative ledger delta). Raises InsufficientUnits. Returns new balance."""
    return await _spend(session, user_id, units, job_id, LedgerReason.RESERVE)


async def charge_revision(session: AsyncSession, user_id: uuid.UUID, job_id: uuid.UUID, units: int) -> int:
    """Charge a paid revision (negative ledger delta). Raises InsufficientUnits. Returns new balance."""
    return await _spend(session, user_id, units, job_id, LedgerReason.REVISION)


async def refund_units(session: AsyncSession, job_id: uuid.UUID) -> int:
    """Refund what the job spent (reserve + revision) minus what was already refunded.

    Idempotent: a second call finds nothing left to refund and returns 0. Returns units refunded.
    """
    user_id = await session.scalar(select(UnitLedger.user_id).where(UnitLedger.job_id == job_id).limit(1))
    if user_id is None:
        return 0
    user = await _lock_user(session, user_id)  # serialises concurrent refunds of the same user's jobs

    rows = await session.execute(
        select(UnitLedger.reason, func.coalesce(func.sum(UnitLedger.delta), 0))
        .where(UnitLedger.job_id == job_id)
        .group_by(UnitLedger.reason)
    )
    totals = {reason: int(total) for reason, total in rows.all()}
    spent = -sum(totals.get(reason, 0) for reason in _SPEND_REASONS)
    refunded = totals.get(LedgerReason.REFUND, 0)
    amount = spent - refunded
    if amount <= 0:
        return 0
    await _apply(session, user, amount, LedgerReason.REFUND, job_id=job_id)
    logger.info("refunded units user_id=%s job_id=%s units=%s", user_id, job_id, amount)
    return amount


def trial_available(user: User) -> bool:
    """ARCHITECTURE section 8: not used yet AND a verified phone (its hash) is on file."""
    return not user.trial_used and user.phone_hash is not None


async def use_trial(session: AsyncSession, user_id: uuid.UUID) -> None:
    # ASSUMPTION: the phone-verified requirement (trial_available) is checked at the API layer, not here.
    user = await _lock_user(session, user_id)
    if user.trial_used:
        raise TrialUnavailable()
    user.trial_used = True
    await session.flush()


async def release_trial(session: AsyncSession, user_id: uuid.UUID) -> None:
    user = await _lock_user(session, user_id)
    user.trial_used = False
    await session.flush()
