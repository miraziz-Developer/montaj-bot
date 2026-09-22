import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models import UnitLedger
from app.models.enums import JobStatus, LedgerReason
from app.services import onboarding as svc
from app.services.billing import trial_available
from app.services.users import get_or_create_from_telegram, hash_phone, set_onboarding
from tests.factories import create_job, create_user

PEPPER = "pepper"


# ---------- pure helpers ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ko‘chmas mulk", "ko‘chmas mulk"),
        ("  a   b\n\tc  ", "a b c"),
        ("x\x00y\x07z", "x y z"),
        ("", None),
        ("   \n ", None),
        ("a" * 100, "a" * 60),
    ],
)
def test_clean_free_text(raw: str, expected: str | None) -> None:
    assert svc.clean_free_text(raw) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("ref_abcd1234", "ABCD1234"),
        ("ref_ABCD1234", "ABCD1234"),
        (None, None),
        ("", None),
        ("abcd1234", None),
        ("ref_", None),
        ("ref_ab", None),
        ("ref_a b c d", None),
        ("ref_" + "A" * 17, None),
    ],
)
def test_parse_referral_code(args: str | None, expected: str | None) -> None:
    assert svc.parse_referral_code(args) == expected


def test_is_own_contact() -> None:
    assert svc.is_own_contact(5, 5)
    assert not svc.is_own_contact(6, 5)
    assert not svc.is_own_contact(None, 5)  # a contact without a Telegram account is never "own"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/grant 123 5", (123, 5)),
        ("/grant@my_bot 123 5", (123, 5)),
        ("/grant   7   10000", (7, 10000)),
    ],
)
def test_parse_grant_args_ok(text: str, expected: tuple[int, int]) -> None:
    assert svc.parse_grant_args(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "/grant",
        "/grant 123",
        "/grant 123 5 6",
        "/grant abc 5",
        "/grant 123 x",
        "/grant 123 0",
        "/grant 123 -3",
        "/grant 0 5",
        "/grant 123 10001",
        "/grants 123 5",
        "grant 123 5",
        "/grant 1.5 2",
    ],
)
def test_parse_grant_args_bad(text: str) -> None:
    with pytest.raises(ValueError):
        svc.parse_grant_args(text)


# ---------- DB-backed ----------


async def test_onboarding_saves_niche_and_purpose(session: AsyncSession) -> None:
    user, _ = await get_or_create_from_telegram(session, 1, "u", "U")
    assert not user.onboarding_completed
    await set_onboarding(session, user, "auto", "reels")
    assert (user.niche, user.purpose, user.onboarding_completed) == ("auto", "reels", True)


async def test_phone_normalization_gives_same_hash() -> None:
    assert hash_phone("+998 90 123-45-67", PEPPER) == hash_phone("998901234567", PEPPER)


async def test_attach_phone_then_duplicate_is_denied(session: AsyncSession) -> None:
    first = await create_user(session)
    second = await create_user(session)
    assert await svc.attach_phone(session, first, "+998 90 123-45-67", PEPPER) == svc.PhoneResult.GRANTED
    assert first.phone_hash == hash_phone("998901234567", PEPPER)
    assert await svc.attach_phone(session, second, "998901234567", PEPPER) == svc.PhoneResult.DENIED
    assert second.phone_hash is None


async def test_attach_phone_is_idempotent_and_never_swaps_numbers(session: AsyncSession) -> None:
    user = await create_user(session)
    await svc.attach_phone(session, user, "998901234567", PEPPER)
    assert await svc.attach_phone(session, user, "998901234567", PEPPER) == svc.PhoneResult.GRANTED
    assert await svc.attach_phone(session, user, "998907654321", PEPPER) == svc.PhoneResult.DENIED
    assert user.phone_hash == hash_phone("998901234567", PEPPER)


async def test_trial_available_rule_and_menu_offer(session: AsyncSession) -> None:
    user = await create_user(session)
    assert not trial_available(user) and svc.trial_offered(user)  # no phone yet: offer the button
    await svc.attach_phone(session, user, "998901234567", PEPPER)
    assert trial_available(user) and not svc.trial_offered(user)
    user.trial_used = True
    assert not trial_available(user) and not svc.trial_offered(user)


async def test_referral_only_for_new_users_and_not_self(session: AsyncSession) -> None:
    inviter, _ = await get_or_create_from_telegram(session, 10, None, None)
    newbie, created = await get_or_create_from_telegram(session, 11, None, None)
    assert created
    assert await svc.apply_referral(session, newbie, created, inviter.referral_code)
    assert newbie.referred_by == inviter.id

    # existing user opening a referral link later: no change
    veteran, _ = await get_or_create_from_telegram(session, 12, None, None)
    assert not await svc.apply_referral(session, veteran, False, inviter.referral_code)
    assert veteran.referred_by is None

    # self-referral, unknown code and missing code are ignored
    solo, created = await get_or_create_from_telegram(session, 13, None, None)
    assert not await svc.apply_referral(session, solo, created, solo.referral_code)
    assert not await svc.apply_referral(session, solo, created, "NOSUCHCODE")
    assert not await svc.apply_referral(session, solo, created, None)
    assert solo.referred_by is None


async def test_grant_by_telegram_id(session: AsyncSession) -> None:
    admin = await create_user(session, is_admin=True)
    target = await create_user(session, telegram_id=555, balance_units=2)
    user, balance = await svc.grant_by_telegram_id(session, admin, 555, 5)
    assert (user.id, balance) == (target.id, 7)
    row = (await session.execute(select(UnitLedger))).scalar_one()
    assert (row.delta, row.reason) == (5, LedgerReason.ADMIN_GRANT)
    assert str(admin.telegram_id) in (row.note or "")


async def test_grant_unknown_user_raises_not_found(session: AsyncSession) -> None:
    admin = await create_user(session, is_admin=True)
    with pytest.raises(NotFound):
        await svc.grant_by_telegram_id(session, admin, 99999, 5)


async def test_collect_stats(session: AsyncSession) -> None:
    admin = await create_user(session, onboarding_completed=True)
    other = await create_user(session)
    await create_job(session, admin.id, status=JobStatus.DONE)
    await create_job(session, admin.id, status=JobStatus.DONE)
    await create_job(session, other.id, status=JobStatus.QUEUED)
    await svc.grant_by_telegram_id(session, admin, other.telegram_id, 4)
    stats = await svc.collect_stats(session)
    assert (stats.total_users, stats.onboarded_users, stats.units_granted) == (2, 1, 4)
    assert stats.jobs_by_status == {"DONE": 2, "QUEUED": 1}
