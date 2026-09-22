"""Tariffs, manual payment, admin approval and /myvideos through the REAL dispatcher and DB."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageText, SendPhoto
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.billing import fmt_uzs, relative_uz, tariffs_text, videos_text
from app.bot.setup import create_dispatcher
from app.core.config import Settings
from app.models import Job, Payment, UnitLedger, User
from app.models.enums import JobStatus, PaymentStatus
from app.services import users as users_service
from tests.fakes.telegram import FakeSession
from tests.integration.test_bot_flow import Harness, _detach_routers, callback_update, message_update

ADMIN_ID = 9000
CARD = "8600 1234 5678 9012 (Test Ism)"


def photo_update(uid: int, file_id: str = "receipt-file") -> object:
    return message_update(
        uid,
        photo=[
            {"file_id": "small", "file_unique_id": "u1", "width": 90, "height": 90},
            {"file_id": file_id, "file_unique_id": "u2", "width": 800, "height": 800},
        ],
    )


@pytest.fixture
async def bot_env(session_factory, monkeypatch):  # noqa: ANN001, ANN201
    settings = Settings(_env_file=None, admin_telegram_ids=[ADMIN_ID], payment_card_text=CARD)
    monkeypatch.setattr(users_service, "get_settings", lambda: settings)
    fake = FakeSession()
    bot = Bot(token="123456:TEST-TOKEN", session=fake)
    _detach_routers()
    dp = create_dispatcher(settings, session_factory, MemoryStorage())
    yield Harness(dp, bot, fake), settings
    _detach_routers()


def _calls(h: Harness, method: type) -> list:
    return [c for c in h.fake.calls if isinstance(c, method)]


async def _user(session: AsyncSession, telegram_id: int) -> User:
    session.expire_all()
    return (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()


async def _payments(session: AsyncSession) -> list[Payment]:
    session.expire_all()
    return list((await session.execute(select(Payment).order_by(Payment.created_at))).scalars().all())


async def _buy(h: Harness, uid: int, code: str = "pro") -> None:
    await h.send(callback_update(uid, f"buy:{code}"))


# ---------- tariffs ----------


async def test_tariffs_screen_lists_all_tariffs_with_the_recommended_tag(bot_env) -> None:  # noqa: ANN001
    h, _ = bot_env
    await h.send(message_update(1, "/tariflar"))
    [sent] = h.fake.sent_to(1)
    assert sent.text == tariffs_text()
    assert sent.text.count("⭐ Tavsiya etiladi") == 1 and "249 000 so‘m\n⭐ Tavsiya etiladi" in sent.text
    assert "1 ta video (3 daqiqagacha)" in sent.text and "100 ta video" in sent.text
    buttons = [b for row in sent.reply_markup.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["buy:single", "buy:start", "buy:pro", "buy:max"]
    assert "99 000" in buttons[1].text

    await h.send(callback_update(1, "menu:tariffs"))  # the menu button shows the same screen
    assert h.fake.sent_to(1)[0].text == tariffs_text()


# ---------- buying ----------


async def test_buy_shows_instructions_with_card_and_exact_amount(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 5, "pro")
    [sent] = h.fake.sent_to(5)
    assert sent.text == texts.PAYMENT_INSTRUCTIONS.format(label="Pro", units=30, price="249 000", card=CARD)
    [payment] = await _payments(session)
    assert (payment.plan_code, payment.amount_uzs, payment.status) == ("pro", 249_000, PaymentStatus.PENDING)
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == f"pay:cancel:{payment.id}"


async def test_unknown_tariff_or_missing_card_creates_nothing(bot_env, session) -> None:  # noqa: ANN001
    h, settings = bot_env
    await _buy(h, 6, "diamond")
    assert h.fake.sent == [] and await _payments(session) == []
    settings.payment_card_text = ""
    await _buy(h, 6, "pro")
    assert h.texts(6) == [texts.CARD_NOT_CONFIGURED] and await _payments(session) == []


async def test_full_flow_buy_receipt_admin_approve(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    uid = 7
    await _buy(h, uid, "start")
    [payment] = await _payments(session)
    payment_id = payment.id

    await h.send(photo_update(uid, "my-receipt"))
    assert h.texts(uid) == [texts.RECEIPT_RECEIVED]
    [to_admin] = _calls(h, SendPhoto)
    assert (to_admin.chat_id, to_admin.photo) == (
        ADMIN_ID,
        "my-receipt",
    )  # the same file_id, nothing re-uploaded
    assert "Tarif: Start — 10 birlik" in to_admin.caption and "Summa: 99 000 so‘m" in to_admin.caption
    assert f"id {uid}" in to_admin.caption and str(payment_id)[:8] in to_admin.caption
    keys = [b.callback_data for row in to_admin.reply_markup.inline_keyboard for b in row]
    assert keys == [f"pay:ok:{payment_id}", f"pay:no:{payment_id}"]
    assert (await _payments(session))[0].receipt_file_id == "my-receipt"

    await h.send(callback_update(ADMIN_ID, f"pay:ok:{payment_id}"))
    payment = (await _payments(session))[0]
    assert payment.status == PaymentStatus.APPROVED and payment.decided_by is not None
    assert (await _user(session, uid)).balance_units == 10
    ledger = (await session.execute(select(UnitLedger))).scalar_one()
    assert (ledger.reason.value, ledger.delta, ledger.payment_id) == ("purchase", 10, payment_id)
    # the user is told, with the new balance
    assert h.texts(uid) == [texts.PAYMENT_APPROVED.format(units=10, balance=10)]
    # the admin's message shows the decision and who made it
    [edit] = _calls(h, EditMessageCaption)
    assert "✅ Tasdiqlandi — User9000" in edit.caption and edit.reply_markup is None


async def test_admin_reject_tells_the_user_and_grants_nothing(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 8)
    await h.send(photo_update(8))
    [payment] = await _payments(session)
    await h.send(callback_update(ADMIN_ID, f"pay:no:{payment.id}"))
    assert (await _payments(session))[0].status == PaymentStatus.REJECTED
    assert (await _user(session, 8)).balance_units == 0
    assert h.texts(8) == [texts.PAYMENT_REJECTED]
    assert "❌ Rad etildi — User9000" in _calls(h, EditMessageCaption)[0].caption


# ---------- security ----------


@pytest.mark.parametrize("action", ["ok", "no"])
async def test_non_admins_cannot_decide_payments_even_by_guessing(
    bot_env, session, caplog, action: str
) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 10)
    await h.send(photo_update(10))
    [payment] = await _payments(session)
    for outsider in (10, 11):  # the paying user themselves, and a stranger
        with caplog.at_level("WARNING"):
            await h.send(callback_update(outsider, f"pay:{action}:{payment.id}"))
        assert h.fake.sent == [] and _calls(h, EditMessageCaption) == []
        assert _calls(h, AnswerCallbackQuery)[0].text is None  # silent
    assert (await _payments(session))[0].status == PaymentStatus.PENDING
    assert (await _user(session, 10)).balance_units == 0
    assert "non-admin" in caplog.text


async def test_a_second_decision_is_refused_without_a_second_grant(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 12)
    await h.send(photo_update(12))
    [payment] = await _payments(session)
    await h.send(callback_update(ADMIN_ID, f"pay:ok:{payment.id}"))
    await h.send(callback_update(ADMIN_ID, f"pay:ok:{payment.id}"))  # double click
    answer = _calls(h, AnswerCallbackQuery)[0]
    assert (answer.text, answer.show_alert) == (texts.PAYMENT_ALREADY_DECIDED, True)
    await h.send(callback_update(ADMIN_ID, f"pay:no:{payment.id}"))  # or a change of mind
    assert (await _user(session, 12)).balance_units == 30
    assert (await _payments(session))[0].status == PaymentStatus.APPROVED


async def test_garbage_payment_ids_are_ignored(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await h.send(callback_update(ADMIN_ID, "pay:ok:not-a-uuid"))
    await h.send(callback_update(ADMIN_ID, "pay:ok:00000000-0000-0000-0000-000000000000"))
    assert h.fake.sent == [] and await _payments(session) == []


# ---------- receipt handling ----------


async def test_a_non_photo_is_refused_and_the_state_is_kept(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 13)
    await h.send(message_update(13, "to‘ladim"))
    assert h.texts(13) == [texts.PLEASE_SEND_PHOTO]
    await h.send(message_update(13, document={"file_id": "d", "file_unique_id": "u"}))
    assert h.texts(13) == [texts.PLEASE_SEND_PHOTO]
    await h.send(photo_update(13))  # still waiting: the photo now works
    assert h.texts(13) == [texts.RECEIPT_RECEIVED]
    assert (await _payments(session))[0].receipt_file_id == "receipt-file"


async def test_commands_are_not_treated_as_receipts(bot_env) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 14)
    await h.send(message_update(14, "/help"))
    assert h.texts(14) == [texts.HELP]


async def test_a_receipt_for_a_payment_that_is_no_longer_pending_is_refused(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 15)
    [payment] = await _payments(session)
    await session.execute(
        update(Payment).where(Payment.id == payment.id).values(status=PaymentStatus.REJECTED)
    )
    await session.commit()
    await h.send(photo_update(15))
    assert h.texts(15) == [texts.PAYMENT_STALE] and _calls(h, SendPhoto) == []


async def test_a_photo_without_a_purchase_is_just_a_photo(bot_env) -> None:  # noqa: ANN001
    h, _ = bot_env
    await h.send(photo_update(16))
    assert _calls(h, SendPhoto) == []  # not in the receipt state: nothing goes to the admins


async def test_no_admins_configured_still_acknowledges_the_user(bot_env, session) -> None:  # noqa: ANN001
    h, settings = bot_env
    settings.admin_telegram_ids = []
    await _buy(h, 17)
    await h.send(photo_update(17))
    assert h.texts(17) == [texts.RECEIPT_RECEIVED] and _calls(h, SendPhoto) == []
    assert (await _payments(session))[0].receipt_file_id == "receipt-file"


# ---------- cancel ----------


async def test_the_user_can_cancel_a_pending_payment(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 18)
    [payment] = await _payments(session)
    await h.send(callback_update(18, f"pay:cancel:{payment.id}"))
    payment = (await _payments(session))[0]
    assert (payment.status, payment.note) == (PaymentStatus.REJECTED, "user_canceled")
    assert _calls(h, EditMessageText)[0].text == texts.PAYMENT_CANCELED
    await h.send(photo_update(18))  # the state was cleared: the photo is not a receipt any more
    assert _calls(h, SendPhoto) == []


async def test_a_stranger_cannot_cancel_someone_elses_payment(bot_env, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await _buy(h, 19)
    [payment] = await _payments(session)
    await h.send(callback_update(20, f"pay:cancel:{payment.id}"))
    assert (await _payments(session))[0].status == PaymentStatus.PENDING


# ---------- /myvideos ----------


async def test_myvideos_is_empty_for_a_new_user(bot_env) -> None:  # noqa: ANN001
    h, _ = bot_env
    await h.send(message_update(21, "/myvideos"))
    assert h.texts(21) == [texts.MYVIDEOS_EMPTY]
    await h.send(callback_update(21, "menu:videos"))
    assert h.texts(21) == [texts.MYVIDEOS_EMPTY]


async def test_myvideos_lists_the_ten_newest_first(bot_env, harness, session) -> None:  # noqa: ANN001
    h, _ = bot_env
    await h.send(message_update(22, "/start"))
    user = await _user(session, 22)
    now = datetime.now(UTC)
    ids = []
    for i in range(12):
        job = await harness.new_job(
            user_id=user.id, reserve=0, status=JobStatus.DONE if i % 2 == 0 else JobStatus.FAILED
        )
        ids.append(job.job_id)
        await session.execute(
            update(Job).where(Job.id == job.job_id).values(created_at=now - timedelta(hours=12 - i))
        )
    await session.commit()
    # the newest job has a plan with a title
    await harness.add_plan(ids[-1])

    await h.send(message_update(22, "/myvideos"))
    [text] = h.texts(22)
    lines = text.split("\n\n", 1)[1].split("\n")
    assert text.startswith(texts.MYVIDEOS_TITLE) and len(lines) == 20  # 10 entries, 2 lines each
    assert lines[0].startswith("1. ") and "Test" in lines[1]  # newest first, titled by its plan
    assert texts.VIDEO_FALLBACK_TITLE.format(short_id=str(ids[-2])[:6]) in lines[3]  # no plan yet: short id
    assert str(ids[0])[:6] not in text and str(ids[1])[:6] not in text  # the two oldest are cut off
    assert "❌ Xatolik" in lines[0]  # the newest job (i=11) failed


async def test_relative_dates_and_price_format() -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    deltas = (
        timedelta(seconds=5),
        timedelta(minutes=7),
        timedelta(hours=3),
        timedelta(hours=30),
        timedelta(days=4),
    )
    assert [relative_uz(now - d, now) for d in deltas] == [
        "hozir", "7 daqiqa oldin", "3 soat oldin", "kecha", "4 kun oldin",
    ]  # fmt: skip
    assert [fmt_uzs(n) for n in (15_000, 99_000, 690_000, 999)] == ["15 000", "99 000", "690 000", "999"]


async def test_every_job_status_has_an_uzbek_name(session: AsyncSession) -> None:
    assert set(texts.JOB_STATUS_UZ) == {s.value for s in JobStatus}
    assert texts.JOB_STATUS_UZ["AWAITING_PLAN_APPROVAL"] == "⏳ Tasdiq kutilmoqda"
    assert videos_text.__name__ == "videos_text"
