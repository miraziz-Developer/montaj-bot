import asyncio
from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import User
from app.services import users
from app.services.users import get_or_create_from_telegram, hash_phone, set_onboarding


async def test_get_or_create_is_idempotent(session: AsyncSession) -> None:
    first, created1 = await get_or_create_from_telegram(session, 777, "bob", "Bob")
    second, created2 = await get_or_create_from_telegram(session, 777, "bobby", "Bobby")
    assert (created1, created2) == (True, False)
    assert first.id == second.id
    assert second.username == "bobby"
    assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_referral_codes_are_unique_and_well_formed(session: AsyncSession) -> None:
    created = [(await get_or_create_from_telegram(session, 5000 + i, None, None))[0] for i in range(30)]
    codes = {u.referral_code for u in created}
    assert len(codes) == 30
    assert all(len(c) == 8 and c.isalnum() and c.isupper() for c in codes)


async def test_concurrent_create_yields_one_user(session_factory: Callable[[], AsyncSession]) -> None:
    async def create() -> tuple[object, bool]:
        async with session_factory() as s:
            user, created = await get_or_create_from_telegram(s, 4242, "x", "X")
            await s.commit()
            return user.id, created

    results = await asyncio.gather(create(), create())
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]


async def test_is_admin_from_settings(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(users, "get_settings", lambda: Settings(_env_file=None, admin_telegram_ids=[900]))
    admin, _ = await get_or_create_from_telegram(session, 900, None, None)
    regular, _ = await get_or_create_from_telegram(session, 901, None, None)
    assert (admin.is_admin, regular.is_admin) == (True, False)


async def test_set_onboarding(session: AsyncSession) -> None:
    user, _ = await get_or_create_from_telegram(session, 31, None, None)
    await set_onboarding(session, user, "fitness", "sales")
    assert (user.niche, user.purpose, user.onboarding_completed) == ("fitness", "sales", True)


def test_hash_phone_normalizes_and_uses_pepper() -> None:
    assert hash_phone("+998 90 123-45-67", "p") == hash_phone("998901234567", "p")
    assert hash_phone("998901234567", "p") != hash_phone("998901234567", "q")
    assert len(hash_phone("998901234567", "p")) == 64


def test_hash_phone_rejects_empty() -> None:
    with pytest.raises(ValueError):
        hash_phone("abc", "p")
