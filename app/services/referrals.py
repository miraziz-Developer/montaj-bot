import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import JobStatus, LedgerReason
from app.models.job import Job
from app.models.ledger import UnitLedger
from app.models.user import User
from app.services import billing
from app.services.tariffs import REFERRAL_BONUS_UNITS

logger = logging.getLogger(__name__)


async def reward_referrer(session: AsyncSession, job: Job) -> int:
    """Give the inviter +3 units when the invited user's FIRST job reaches DONE (anti-fraud rule).

    Call inside the transaction that moved `job` to DONE. Idempotent. Returns the units granted (0 = none).
    """
    invited = await session.get(User, job.user_id)
    if invited is None or invited.referred_by is None:
        return 0
    done_jobs = await session.scalar(
        select(func.count()).select_from(Job).where(Job.user_id == invited.id, Job.status == JobStatus.DONE)
    )
    if done_jobs != 1:  # not the first successful job
        return 0
    note = f"referral:{invited.id}"
    already = await session.scalar(
        select(UnitLedger.id).where(
            UnitLedger.user_id == invited.referred_by,
            UnitLedger.reason == LedgerReason.REFERRAL,
            UnitLedger.note == note,
        )
    )
    if already is not None:
        return 0
    await billing.grant_units(
        session, invited.referred_by, REFERRAL_BONUS_UNITS, LedgerReason.REFERRAL, job_id=job.id, note=note
    )
    logger.info(
        "referral reward inviter=%s invited=%s units=%s",
        invited.referred_by,
        invited.id,
        REFERRAL_BONUS_UNITS,
    )
    return REFERRAL_BONUS_UNITS
