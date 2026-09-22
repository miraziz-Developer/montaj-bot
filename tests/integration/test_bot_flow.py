"""Drives the REAL dispatcher/handlers/DB with fake Telegram updates (no network, no aiogram internals)."""

import itertools
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardRemove, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers import admin, billing, fallback, menu, plans, start
from app.bot.setup import create_dispatcher
from app.core.config import Settings
from app.models import User
from app.services import users as users_service
from tests.fakes.telegram import FakeSession

_update_ids = itertools.count(1)
ADMIN_ID = 9000
HTTPS = "https://montaj.example.com"


def _from(uid: int) -> dict[str, Any]:
    return {"id": uid, "is_bot": False, "first_name": f"User{uid}", "username": f"user{uid}"}


def message_update(uid: int, text: str | None = None, *, chat_type: str = "private", **extra: Any) -> Update:
    message = {
        "message_id": next(_update_ids),
        "date": 1_700_000_000,
        "chat": {"id": uid, "type": chat_type},
        "from": _from(uid),
        **extra,
    }
    if text is not None:
        message["text"] = text
    return Update.model_validate({"update_id": next(_update_ids), "message": message})


def callback_update(uid: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": next(_update_ids),
            "callback_query": {
                "id": str(next(_update_ids)),
                "from": _from(uid),
                "chat_instance": "ci",
                "data": data,
                "message": {"message_id": 1, "date": 1_700_000_000, "chat": {"id": uid, "type": "private"}},
            },
        }
    )


def contact_update(uid: int, *, contact_user_id: int | None, phone: str = "+998901234567") -> Update:
    contact = {"phone_number": phone, "first_name": "X"}
    if contact_user_id is not None:
        contact["user_id"] = contact_user_id
    return message_update(uid, contact=contact)


class Harness:
    def __init__(self, dp: Dispatcher, bot: Bot, session: FakeSession) -> None:
        self.dp, self.bot, self.fake = dp, bot, session

    async def send(self, update: Update) -> None:
        self.fake.clear()
        await self.dp.feed_update(self.bot, update)

    def texts(self, uid: int) -> list[str]:
        return self.fake.texts_to(uid)


def _detach_routers() -> None:
    """Module-level routers can join only one Dispatcher; production builds one, each test builds its own."""
    for router in (start.router, plans.router, billing.router, menu.router, admin.router, fallback.router):
        parent = router.parent_router
        if parent is not None:
            parent.sub_routers.remove(router)
            router._parent_router = None


@pytest.fixture
async def bot_env(
    session_factory: Callable[[], AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Harness]:
    settings = Settings(
        _env_file=None,
        public_base_url=HTTPS,
        admin_telegram_ids=[ADMIN_ID],
        phone_hash_pepper="test-pepper",
    )
    monkeypatch.setattr(users_service, "get_settings", lambda: settings)
    fake = FakeSession()
    bot = Bot(token="123456:TEST-TOKEN", session=fake)
    _detach_routers()
    dp = create_dispatcher(settings, session_factory, MemoryStorage())
    yield Harness(dp, bot, fake)
    _detach_routers()


async def _user(session: AsyncSession, telegram_id: int) -> User:
    session.expire_all()
    return (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()


async def _complete_onboarding(h: Harness, uid: int) -> None:
    await h.send(message_update(uid, "/start"))
    await h.send(callback_update(uid, "onb:start"))
    await h.send(callback_update(uid, "onb:niche:auto"))
    await h.send(callback_update(uid, "onb:purpose:reels"))


# ---------- /start and onboarding ----------


async def test_start_greets_new_user_with_begin_button(bot_env: Harness, session: AsyncSession) -> None:
    await bot_env.send(message_update(1, "/start"))
    [reply] = bot_env.fake.sent_to(1)
    assert reply.text == texts.WELCOME.format(name="User1")
    assert reply.reply_markup.inline_keyboard[0][0].callback_data == "onb:start"
    user = await _user(session, 1)
    assert not user.onboarding_completed


async def test_full_onboarding_with_preset_choices(bot_env: Harness, session: AsyncSession) -> None:
    uid = 2
    await bot_env.send(message_update(uid, "/start"))
    await bot_env.send(callback_update(uid, "onb:start"))
    assert bot_env.texts(uid) == [texts.ASK_NICHE]
    await bot_env.send(callback_update(uid, "onb:niche:auto"))
    assert bot_env.texts(uid) == [texts.ASK_PURPOSE]
    await bot_env.send(callback_update(uid, "onb:purpose:reels"))
    [ask] = bot_env.fake.sent_to(uid)
    assert ask.text == texts.ASK_CONTACT
    assert ask.reply_markup.keyboard[0][0].request_contact is True

    user = await _user(session, uid)
    assert (user.niche, user.purpose, user.onboarding_completed) == ("auto", "reels", True)


async def test_free_text_niche_and_purpose_are_cleaned_and_truncated(
    bot_env: Harness, session: AsyncSession
) -> None:
    uid = 3
    await bot_env.send(message_update(uid, "/start"))
    await bot_env.send(callback_update(uid, "onb:niche:other"))
    assert bot_env.texts(uid) == [texts.ASK_NICHE_OTHER]
    await bot_env.send(message_update(uid, "  ko‘chmas   mulk " + "x" * 100))
    assert bot_env.texts(uid) == [texts.ASK_PURPOSE]
    await bot_env.send(callback_update(uid, "onb:purpose:other"))
    assert bot_env.texts(uid) == [texts.ASK_PURPOSE_OTHER]
    await bot_env.send(message_update(uid, "Yangi kanal"))
    assert bot_env.texts(uid) == [texts.ASK_CONTACT]

    user = await _user(session, uid)
    assert user.niche.startswith("ko‘chmas mulk x") and len(user.niche) == 60
    assert user.purpose == "Yangi kanal"


async def test_commands_are_not_swallowed_by_free_text_states(bot_env: Harness) -> None:
    uid = 4
    await bot_env.send(message_update(uid, "/start"))
    await bot_env.send(callback_update(uid, "onb:niche:other"))
    await bot_env.send(message_update(uid, "/help"))
    assert bot_env.texts(uid) == [texts.HELP]


async def test_stale_or_invalid_buttons_are_ignored(bot_env: Harness, session: AsyncSession) -> None:
    uid = 5
    await _complete_onboarding(bot_env, uid)
    await bot_env.send(callback_update(uid, "onb:niche:shop"))  # already onboarded
    assert bot_env.fake.sent == []
    await bot_env.send(callback_update(uid, "onb:purpose:bogus"))
    assert bot_env.fake.sent == []
    user = await _user(session, uid)
    assert user.niche == "auto"


async def test_lost_fsm_data_restarts_questions(bot_env: Harness, session: AsyncSession) -> None:
    uid = 6
    await bot_env.send(message_update(uid, "/start"))
    await bot_env.send(callback_update(uid, "onb:purpose:reels"))  # niche was never chosen
    assert bot_env.texts(uid) == [texts.ASK_NICHE]
    assert not (await _user(session, uid)).onboarding_completed


# ---------- contact / trial ----------


async def test_own_contact_grants_trial_and_shows_menu(bot_env: Harness, session: AsyncSession) -> None:
    uid = 7
    await _complete_onboarding(bot_env, uid)
    await bot_env.send(contact_update(uid, contact_user_id=uid))
    granted, menu = bot_env.fake.sent_to(uid)
    assert granted.text == texts.TRIAL_GRANTED
    assert isinstance(granted.reply_markup, ReplyKeyboardRemove)
    assert menu.text == texts.MENU_TITLE
    assert menu.reply_markup.inline_keyboard[0][0].web_app.url == f"{HTTPS}/app/"
    assert (await _user(session, uid)).phone_hash is not None
    # trial is now active, so the menu no longer offers the activation button
    assert "trial:start" not in [b.callback_data for r in menu.reply_markup.inline_keyboard for b in r]


async def test_someone_elses_contact_is_rejected(bot_env: Harness, session: AsyncSession) -> None:
    uid = 8
    await _complete_onboarding(bot_env, uid)
    await bot_env.send(contact_update(uid, contact_user_id=uid + 1000))
    assert bot_env.texts(uid) == [texts.WRONG_CONTACT]
    await bot_env.send(contact_update(uid, contact_user_id=None))
    assert bot_env.texts(uid) == [texts.WRONG_CONTACT]
    assert (await _user(session, uid)).phone_hash is None


async def test_duplicate_phone_denies_trial(bot_env: Harness, session: AsyncSession) -> None:
    await _complete_onboarding(bot_env, 20)
    await bot_env.send(contact_update(20, contact_user_id=20, phone="+998 90 111-22-33"))
    await _complete_onboarding(bot_env, 21)
    await bot_env.send(contact_update(21, contact_user_id=21, phone="998901112233"))
    assert bot_env.texts(21)[0] == texts.TRIAL_DENIED
    assert (await _user(session, 21)).phone_hash is None


async def test_later_skips_and_menu_offers_trial_button(bot_env: Harness, session: AsyncSession) -> None:
    uid = 9
    await _complete_onboarding(bot_env, uid)
    await bot_env.send(message_update(uid, texts.BTN_LATER))
    skipped, menu = bot_env.fake.sent_to(uid)
    assert skipped.text == texts.TRIAL_SKIPPED
    callbacks = [b.callback_data for r in menu.reply_markup.inline_keyboard for b in r]
    assert "trial:start" in callbacks

    await bot_env.send(callback_update(uid, "trial:start"))
    [ask] = bot_env.fake.sent_to(uid)
    assert ask.text == texts.ASK_CONTACT
    await bot_env.send(contact_update(uid, contact_user_id=uid, phone="+998 90 555-44-33"))
    assert bot_env.texts(uid)[0] == texts.TRIAL_GRANTED


async def test_contact_before_onboarding_is_refused(bot_env: Harness, session: AsyncSession) -> None:
    uid = 10
    await bot_env.send(message_update(uid, "/start"))
    await bot_env.send(contact_update(uid, contact_user_id=uid))
    assert bot_env.texts(uid) == [texts.NEED_START]
    assert (await _user(session, uid)).phone_hash is None


# ---------- menu, balance, fallbacks ----------


async def test_start_for_onboarded_user_shows_menu(bot_env: Harness) -> None:
    await _complete_onboarding(bot_env, 11)
    await bot_env.send(message_update(11, "/start"))
    assert bot_env.texts(11) == [texts.MENU_TITLE]


async def test_balance_command_and_button(bot_env: Harness, session: AsyncSession) -> None:
    uid = 12
    await _complete_onboarding(bot_env, uid)
    (await _user(session, uid)).balance_units = 4
    await session.commit()
    await bot_env.send(message_update(uid, "/balance"))
    assert bot_env.texts(uid) == ["💰 Balansingiz: 4 birlik.\n1 birlik = 3 daqiqagacha video."]

    await bot_env.send(contact_update(uid, contact_user_id=uid))
    await bot_env.send(callback_update(uid, "menu:balance"))
    assert bot_env.texts(uid) == [
        "💰 Balansingiz: 4 birlik.\n1 birlik = 3 daqiqagacha video.\n🎁 Bepul sinov mavjud."
    ]


async def test_help_and_cancel(bot_env: Harness) -> None:
    uid = 13
    await _complete_onboarding(bot_env, uid)
    await bot_env.send(message_update(uid, "/help"))
    assert bot_env.texts(uid) == [texts.HELP]
    await bot_env.send(message_update(uid, "/cancel"))
    assert bot_env.texts(uid) == [texts.CANCELED, texts.MENU_TITLE]


async def test_video_or_document_in_chat_points_to_mini_app(bot_env: Harness) -> None:
    uid = 14
    await _complete_onboarding(bot_env, uid)
    await bot_env.send(
        message_update(
            uid, video={"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1, "duration": 1}
        )
    )
    assert bot_env.texts(uid) == [texts.NO_VIDEO_IN_CHAT]
    await bot_env.send(message_update(uid, document={"file_id": "f", "file_unique_id": "u"}))
    assert bot_env.texts(uid) == [texts.NO_VIDEO_IN_CHAT]


async def test_random_text_gets_hint_or_start_prompt(bot_env: Harness) -> None:
    await bot_env.send(message_update(15, "salom"))
    assert bot_env.texts(15) == [texts.NEED_START]
    await _complete_onboarding(bot_env, 16)
    await bot_env.send(message_update(16, "salom"))
    assert bot_env.texts(16) == [texts.HINT]


async def test_group_chats_are_ignored(bot_env: Harness, session: AsyncSession) -> None:
    await bot_env.send(message_update(17, "/start", chat_type="group"))
    assert bot_env.fake.sent == []
    assert (await session.execute(select(User).where(User.telegram_id == 17))).first() is None


# ---------- referral ----------


async def test_referral_link_only_counts_for_new_users(bot_env: Harness, session: AsyncSession) -> None:
    await bot_env.send(message_update(30, "/start"))
    code = (await _user(session, 30)).referral_code
    await bot_env.send(message_update(31, f"/start ref_{code}"))
    inviter_id = (await _user(session, 30)).id
    assert (await _user(session, 31)).referred_by == inviter_id

    await bot_env.send(message_update(30, f"/start ref_{code}"))  # self
    assert (await _user(session, 30)).referred_by is None
    await bot_env.send(message_update(32, "/start"))
    await bot_env.send(message_update(32, f"/start ref_{code}"))  # existing user
    assert (await _user(session, 32)).referred_by is None


# ---------- admin ----------


async def test_admin_grant_updates_balance_and_notifies_user(bot_env: Harness, session: AsyncSession) -> None:
    await bot_env.send(message_update(40, "/start"))
    await bot_env.send(message_update(ADMIN_ID, "/grant 40 5"))
    assert bot_env.texts(ADMIN_ID) == ["✅ 40 hisobiga 5 birlik qo‘shildi. Yangi balans: 5"]
    assert bot_env.texts(40) == ["🎁 Hisobingizga 5 birlik qo‘shildi."]
    assert (await _user(session, 40)).balance_units == 5


async def test_admin_grant_bad_input_and_unknown_user(bot_env: Harness) -> None:
    await bot_env.send(message_update(ADMIN_ID, "/grant nope"))
    assert bot_env.texts(ADMIN_ID) == [texts.GRANT_USAGE]
    await bot_env.send(message_update(ADMIN_ID, "/grant 99999 5"))
    assert bot_env.texts(ADMIN_ID) == [texts.USER_NOT_FOUND]


async def test_non_admin_gets_no_reply_and_no_grant(bot_env: Harness, session: AsyncSession) -> None:
    await bot_env.send(message_update(41, "/start"))
    await bot_env.send(message_update(41, "/grant 41 100"))
    await bot_env.send(message_update(41, "/stats"))
    assert bot_env.fake.sent == []
    assert (await _user(session, 41)).balance_units == 0


async def test_admin_stats(bot_env: Harness) -> None:
    await bot_env.send(message_update(42, "/start"))
    await bot_env.send(message_update(ADMIN_ID, "/stats"))
    [stats] = bot_env.texts(ADMIN_ID)
    assert "Foydalanuvchilar: 2" in stats  # user 42 + the admin
    assert "Ro‘yxatdan o‘tganlar: 0" in stats
    assert "Berilgan birliklar: 0" in stats
