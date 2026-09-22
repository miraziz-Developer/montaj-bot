import hashlib
import logging
import re
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.user import User

logger = logging.getLogger(__name__)

# No 0/O/1/I: referral codes are read aloud and typed by hand.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_MAX_CREATE_ATTEMPTS = 5


def _new_referral_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def hash_phone(phone: str, pepper: str) -> str:
    """sha256 of the digits of `phone` + pepper (so "+998 90 123-45-67" == "998901234567")."""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        raise ValueError("phone has no digits")
    return hashlib.sha256((digits + pepper).encode()).hexdigest()


async def _find_by_telegram_id(session: AsyncSession, tg_id: int) -> User | None:
    return (await session.execute(select(User).where(User.telegram_id == tg_id))).scalar_one_or_none()


async def get_or_create_from_telegram(
    session: AsyncSession, tg_id: int, username: str | None, first_name: str | None
) -> tuple[User, bool]:
    """Return (user, created). Safe under concurrent calls for the same telegram id."""
    is_admin = tg_id in get_settings().admin_telegram_ids

    existing = await _find_by_telegram_id(session, tg_id)
    if existing is not None:
        existing.username = username
        existing.first_name = first_name
        existing.is_admin = is_admin
        await session.flush()
        return existing, False

    for _ in range(_MAX_CREATE_ATTEMPTS):
        user = User(
            telegram_id=tg_id,
            username=username,
            first_name=first_name,
            referral_code=_new_referral_code(),
            is_admin=is_admin,
        )
        try:
            async with session.begin_nested():
                session.add(user)
                await session.flush()
        except IntegrityError:
            # Either another request created this telegram id first, or the referral code collided.
            raced = await _find_by_telegram_id(session, tg_id)
            if raced is not None:
                return raced, False
            continue
        logger.info("user created telegram_id=%s user_id=%s", tg_id, user.id)
        return user, True
    raise RuntimeError("could not generate a unique referral code")


async def set_onboarding(session: AsyncSession, user: User, niche: str, purpose: str) -> User:
    user.niche = niche
    user.purpose = purpose
    user.onboarding_completed = True
    await session.flush()
    return user
