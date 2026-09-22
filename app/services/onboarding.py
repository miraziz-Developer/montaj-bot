"""Onboarding, phone verification, referral and admin helpers. Pure logic: no Telegram imports."""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models.enums import LedgerReason
from app.models.job import Job
from app.models.ledger import UnitLedger
from app.models.user import User
from app.services import billing
from app.services.users import hash_phone

logger = logging.getLogger(__name__)

NICHE_KEYS = ("auto", "shop", "edu", "food", "beauty", "blog", "other")
PURPOSE_KEYS = ("reels", "youtube", "telegram", "ads", "other")
FREE_TEXT_MAX_LEN = 60
GRANT_MAX_UNITS = 10_000

_REFERRAL_RE = re.compile(r"^ref_([A-Za-z0-9]{4,16})$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_GRANT_COMMAND = re.compile(r"^/grant(@\w+)?$")


class PhoneResult(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class Stats:
    total_users: int
    onboarded_users: int
    jobs_by_status: dict[str, int]
    units_granted: int


def clean_free_text(text: str, max_len: int = FREE_TEXT_MAX_LEN) -> str | None:
    """Sanitize user-typed niche/purpose: strip control chars, collapse spaces, truncate. None if empty."""
    cleaned = " ".join(_CONTROL_CHARS.sub(" ", text).split())[:max_len].strip()
    return cleaned or None


def parse_referral_code(start_args: str | None) -> str | None:
    """`/start ref_ABC123` -> "ABC123" (referral codes are stored upper-case)."""
    match = _REFERRAL_RE.match((start_args or "").strip())
    return match.group(1).upper() if match else None


def is_own_contact(contact_user_id: int | None, sender_id: int) -> bool:
    return contact_user_id is not None and contact_user_id == sender_id


def trial_offered(user: User) -> bool:
    """Menu shows the "Bepul sinov" button while the phone is not verified and the trial is unused."""
    return user.phone_hash is None and not user.trial_used


async def apply_referral(session: AsyncSession, user: User, created: bool, code: str | None) -> bool:
    """Set `referred_by` for a NEW user with a valid code that is not their own. Returns True if set."""
    if not created or code is None:
        return False
    inviter = (await session.execute(select(User).where(User.referral_code == code))).scalar_one_or_none()
    if inviter is None or inviter.id == user.id:
        return False
    user.referred_by = inviter.id
    await session.flush()
    return True


async def attach_phone(session: AsyncSession, user: User, phone: str, pepper: str) -> PhoneResult:
    """Store the phone hash. DENIED if another account already verified the same number."""
    phone_hash = hash_phone(phone, pepper)
    if user.phone_hash is not None:  # already verified: never swap numbers (trial abuse)
        return PhoneResult.GRANTED if user.phone_hash == phone_hash else PhoneResult.DENIED
    taken = await session.scalar(select(User.id).where(User.phone_hash == phone_hash, User.id != user.id))
    if taken is not None:
        return PhoneResult.DENIED
    try:
        async with session.begin_nested():
            user.phone_hash = phone_hash
            await session.flush()
    except IntegrityError:  # lost a race with another account using the same number
        user.phone_hash = None
        return PhoneResult.DENIED
    logger.info("phone verified user_id=%s", user.id)
    return PhoneResult.GRANTED


def parse_grant_args(text: str) -> tuple[int, int]:
    """`/grant <telegram_id> <units>` -> (telegram_id, units). Raises ValueError on bad input."""
    parts = text.split()
    if len(parts) != 3 or not _GRANT_COMMAND.match(parts[0]):
        raise ValueError("usage")
    telegram_id, units = int(parts[1]), int(parts[2])
    if telegram_id <= 0 or not 1 <= units <= GRANT_MAX_UNITS:
        raise ValueError("out of range")
    return telegram_id, units


async def grant_by_telegram_id(
    session: AsyncSession, admin: User, telegram_id: int, units: int
) -> tuple[User, int]:
    """Admin grant. Returns (target user, new balance). Raises NotFound for an unknown telegram id."""
    target = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if target is None:
        raise NotFound()
    balance = await billing.grant_units(
        session,
        target.id,
        units,
        LedgerReason.ADMIN_GRANT,
        note=f"granted by telegram_id={admin.telegram_id}",
    )
    return target, balance


async def collect_stats(session: AsyncSession) -> Stats:
    total = await session.scalar(select(func.count()).select_from(User))
    onboarded = await session.scalar(
        select(func.count()).select_from(User).where(User.onboarding_completed.is_(True))
    )
    rows = await session.execute(select(Job.status, func.count()).group_by(Job.status))
    granted = await session.scalar(
        select(func.coalesce(func.sum(UnitLedger.delta), 0)).where(
            UnitLedger.reason.in_([LedgerReason.PURCHASE, LedgerReason.ADMIN_GRANT, LedgerReason.REFERRAL])
        )
    )
    return Stats(
        total_users=int(total or 0),
        onboarded_users=int(onboarded or 0),
        jobs_by_status={str(status.value): int(count) for status, count in rows.all()},
        units_granted=int(granted or 0),
    )
