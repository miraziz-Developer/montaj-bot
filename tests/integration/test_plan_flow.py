"""Plan approval buttons, revision text and the TelegramNotifier, through the REAL dispatcher and DB."""

from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
from sqlalchemy import update

from app.bot import keyboards, texts
from app.bot.notifier import TelegramNotifier
from app.bot.plan_view import MAX_LISTED_CLIPS, TELEGRAM_TEXT_LIMIT, mmss, plan_message, timeline_lines
from app.bot.setup import create_dispatcher
from app.models import Job
from app.models.enums import JobStatus
from app.schemas.edit_plan import Watermark
from app.services import users as users_service
from tests.fakes.bot import bad_request
from tests.fakes.gemini import make_plan
from tests.fakes.telegram import FakeSession
from tests.integration.test_bot_flow import Harness, _detach_routers, callback_update, message_update
from tests.worker_harness import good_plan

AWAITING = JobStatus.AWAITING_PLAN_APPROVAL


class PlanEnv:
    def __init__(self, harness, bot_harness: Harness, enqueued: list) -> None:  # noqa: ANN001
        self.h, self.bot, self.enqueued = harness, bot_harness, enqueued

    async def job(self, **kw):  # noqa: ANN003, ANN202
        job = await self.h.new_job(**kw)
        job.telegram_id = (await self.h.user(job.user_id)).telegram_id
        return job

    async def click(self, telegram_id: int, action: str, job_id) -> None:  # noqa: ANN001
        await self.bot.send(callback_update(telegram_id, f"plan:{action}:{job_id}"))

    def calls(self, method: type) -> list:
        return [c for c in self.bot.fake.calls if isinstance(c, method)]

    async def set_status_message(self, job_id, message_id: int) -> None:  # noqa: ANN001
        async with self.h.session_factory() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(status_message_id=message_id))
            await session.commit()


@pytest.fixture
async def env(harness, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(users_service, "get_settings", lambda: harness.settings)
    enqueued: list[tuple] = []

    async def enqueue(name: str, *args) -> None:  # noqa: ANN002
        enqueued.append((name, *args))

    fake = FakeSession()
    bot = Bot(token="123456:TEST-TOKEN", session=fake)
    _detach_routers()
    dp = create_dispatcher(harness.settings, harness.session_factory, MemoryStorage(), enqueue=enqueue)
    yield PlanEnv(harness, Harness(dp, bot, fake), enqueued)
    _detach_routers()


# ---------- approve ----------


async def test_owner_approve_enqueues_the_render_once_and_edits_the_message(env: PlanEnv) -> None:
    job = await env.job(status=AWAITING, current_plan_version=1)
    await env.set_status_message(job.job_id, 555)
    await env.click(job.telegram_id, "approve", job.job_id)

    assert env.enqueued == [("run_render", str(job.job_id))]
    [edit] = env.calls(EditMessageText)
    assert (edit.message_id, edit.text, edit.reply_markup) == (555, texts.PROGRESS_PREPARING, None)
    assert env.calls(SendMessage) == []  # edited in place, nothing new sent
    assert env.calls(AnswerCallbackQuery)[0].text == texts.CB_STARTED


async def test_approve_from_another_user_does_nothing(env: PlanEnv) -> None:
    job = await env.job(status=AWAITING, current_plan_version=1)
    stranger = await env.job()
    await env.click(stranger.telegram_id, "approve", job.job_id)
    assert env.enqueued == []
    assert env.calls(EditMessageText) == [] and env.calls(SendMessage) == []
    assert env.calls(AnswerCallbackQuery)[0].text is None  # a silent answer: nothing leaks
    assert (await env.h.job(job.job_id)).status == AWAITING


@pytest.mark.parametrize(
    "payload", ["approve:not-a-uuid", "approve:", "approve:00000000-0000-0000-0000-000000000000"]
)
async def test_garbage_or_unknown_job_ids_are_ignored(env: PlanEnv, payload: str) -> None:
    user = await env.job()
    await env.bot.send(callback_update(user.telegram_id, f"plan:{payload}"))
    assert env.enqueued == [] and env.calls(EditMessageText) == []


async def test_approve_in_the_wrong_state_offers_a_refresh_button(env: PlanEnv) -> None:
    job = await env.job(status=JobStatus.RENDERING)
    await env.click(job.telegram_id, "approve", job.job_id)
    assert env.enqueued == []
    assert env.calls(AnswerCallbackQuery)[0].text == texts.JOB_WRONG_STATE
    [markup] = env.calls(EditMessageReplyMarkup)
    assert markup.reply_markup.inline_keyboard[0][0].callback_data == f"plan:show:{job.job_id}"


async def test_a_failing_queue_does_not_lose_the_buttons(env: PlanEnv) -> None:
    async def broken(*_a) -> None:  # noqa: ANN002
        raise RuntimeError("redis down")

    env.bot.dp["enqueue"] = broken
    job = await env.job(status=AWAITING, current_plan_version=1)
    await env.click(job.telegram_id, "approve", job.job_id)
    assert env.calls(EditMessageText) == []
    answer = env.calls(AnswerCallbackQuery)[0]
    assert (answer.text, answer.show_alert) == (texts.ENQUEUE_FAILED, True)


# ---------- revise ----------


async def test_revise_asks_then_enqueues_the_typed_text(env: PlanEnv) -> None:
    job = await env.job(status=AWAITING, current_plan_version=1)
    await env.set_status_message(job.job_id, 9)
    await env.click(job.telegram_id, "revise", job.job_id)
    assert [c.text for c in env.calls(SendMessage)] == [texts.ASK_REVISION]

    await env.bot.send(message_update(job.telegram_id, "boshini qisqartir va musiqani o‘chir"))
    assert env.enqueued == [("run_revision", str(job.job_id), "boshini qisqartir va musiqani o‘chir")]
    [edit] = env.calls(EditMessageText)
    assert (edit.message_id, edit.text) == (9, texts.REVISION_QUEUED)


async def test_the_revision_text_is_cut_to_500_characters(env: PlanEnv) -> None:
    job = await env.job(status=AWAITING, current_plan_version=1)
    await env.click(job.telegram_id, "revise", job.job_id)
    await env.bot.send(message_update(job.telegram_id, "x" * 900))
    assert len(env.enqueued[0][2]) == 500


async def test_revision_text_after_the_job_moved_on_is_refused(env: PlanEnv) -> None:
    job = await env.job(status=AWAITING, current_plan_version=1)
    await env.click(job.telegram_id, "revise", job.job_id)
    await env.h.set_status(job.job_id, JobStatus.RENDERING)  # the user approved on another device meanwhile
    await env.bot.send(message_update(job.telegram_id, "qisqartir"))
    assert env.enqueued == []
    assert [c.text for c in env.calls(SendMessage)] == [texts.JOB_WRONG_STATE]


async def test_revise_from_a_stranger_and_commands_in_revision_state(env: PlanEnv) -> None:
    job = await env.job(status=AWAITING, current_plan_version=1)
    stranger = await env.job()
    await env.click(stranger.telegram_id, "revise", job.job_id)
    assert env.calls(SendMessage) == []

    await env.click(job.telegram_id, "revise", job.job_id)
    await env.bot.send(message_update(job.telegram_id, "/help"))  # a command is not a revision request
    assert env.enqueued == [] and [c.text for c in env.calls(SendMessage)] == [texts.HELP]


# ---------- cancel ----------


async def test_cancel_while_queued_refunds_units_through_the_shared_function(env: PlanEnv) -> None:
    job = await env.job(status=JobStatus.QUEUED, balance=5, reserve=1)
    await env.click(job.telegram_id, "cancel", job.job_id)
    assert (await env.h.job(job.job_id)).status == JobStatus.CANCELED
    assert await env.h.ledger(job.user_id) == [("reserve", -1), ("refund", 1)]
    assert (await env.h.user(job.user_id)).balance_units == 5
    assert [c.text for c in env.calls(SendMessage)] == [texts.JOB_CANCELED]  # no status message yet: sent


async def test_cancel_while_awaiting_approval_does_not_refund(env: PlanEnv) -> None:
    job = await env.job(status=AWAITING, current_plan_version=1)
    await env.set_status_message(job.job_id, 12)
    await env.click(job.telegram_id, "cancel", job.job_id)
    assert (await env.h.job(job.job_id)).status == JobStatus.CANCELED
    assert await env.h.ledger(job.user_id) == [("reserve", -1)]  # the compute is spent
    [edit] = env.calls(EditMessageText)
    assert (edit.message_id, edit.text) == (12, texts.JOB_CANCELED)


async def test_cancel_of_a_queued_trial_job_gives_the_trial_back(env: PlanEnv) -> None:
    job = await env.job(status=JobStatus.QUEUED, is_trial=True, balance=0)
    await env.click(job.telegram_id, "cancel", job.job_id)
    assert (await env.h.user(job.user_id)).trial_used is False


async def test_cancel_by_a_stranger_or_in_a_running_state_changes_nothing(env: PlanEnv) -> None:
    job = await env.job(status=JobStatus.QUEUED)
    stranger = await env.job()
    await env.click(stranger.telegram_id, "cancel", job.job_id)
    assert (await env.h.job(job.job_id)).status == JobStatus.QUEUED

    running = await env.job(status=JobStatus.RENDERING)
    await env.click(running.telegram_id, "cancel", running.job_id)
    assert (await env.h.job(running.job_id)).status == JobStatus.RENDERING
    assert env.calls(AnswerCallbackQuery)[-1].text == texts.JOB_WRONG_STATE


def test_api_and_bot_share_one_cancel_implementation() -> None:
    for path in ("app/api/routers/jobs.py", "app/bot/handlers/plans.py"):
        source = Path(path).read_text()
        assert "jobs.cancel_job(" in source, path
        assert "refund_units" not in source and "release_trial" not in source, (
            f"cancel logic duplicated in {path}"
        )


# ---------- plan:show ----------


async def test_show_resends_the_current_plan_or_reports_the_state(env: PlanEnv) -> None:
    job = await env.job(status=AWAITING, current_plan_version=1)
    await env.h.add_plan(job.job_id, good_plan())
    await env.click(job.telegram_id, "show", job.job_id)
    [sent] = env.calls(SendMessage)
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == f"plan:approve:{job.job_id}"

    running = await env.job(status=JobStatus.RENDERING)
    await env.click(running.telegram_id, "show", running.job_id)
    answer = env.calls(AnswerCallbackQuery)[-1]
    assert answer.text == texts.JOB_STATUS_NOW.format(status="RENDERING") and answer.show_alert


# ---------- TelegramNotifier ----------


@pytest.fixture
def notifier_env(harness):  # noqa: ANN001, ANN201
    fake = FakeSession()
    bot = Bot(token="123456:TEST-TOKEN", session=fake)
    return harness, fake, TelegramNotifier(bot, harness.session_factory)


async def test_plan_ready_twice_edits_the_same_message(notifier_env) -> None:  # noqa: ANN001
    harness, fake, notifier = notifier_env
    h = await harness.new_job(status=AWAITING, current_plan_version=1)
    plan = good_plan()
    await notifier.plan_ready(await harness.job(h.job_id), plan)
    await notifier.plan_ready(await harness.job(h.job_id), make_plan([(0.0, 3.0)]), ["qisqartirildi"], [])

    sends = [c for c in fake.calls if isinstance(c, SendMessage)]
    edits = [c for c in fake.calls if isinstance(c, EditMessageText)]
    assert len(sends) == 1 and len(edits) == 1  # ONE message for the whole job
    stored = (await harness.job(h.job_id)).status_message_id
    assert stored is not None and edits[0].message_id == stored
    assert sends[0].reply_markup.inline_keyboard[0][0].text == texts.BTN_APPROVE
    assert edits[0].text.startswith(f"{texts.PLAN_CHANGES}\n• qisqartirildi")


async def test_progress_failed_and_delivered_edit_the_status_message(notifier_env) -> None:  # noqa: ANN001
    harness, fake, notifier = notifier_env
    h = await harness.new_job(status=JobStatus.QUEUED)
    job = await harness.job(h.job_id)
    await notifier.progress(job, texts.PROGRESS_PREPARING)
    await notifier.failed(job, texts.FAILED_STT)
    await notifier.delivered(job)
    sends = [c for c in fake.calls if isinstance(c, SendMessage)]
    edits = [c for c in fake.calls if isinstance(c, EditMessageText)]
    assert [s.text for s in sends] == [texts.PROGRESS_PREPARING]
    assert edits[0].text == f"❌ {texts.FAILED_STT}\n\n{texts.FAILED_RETRY_HINT}"
    assert edits[1].text == texts.DELIVERED_DONE
    assert all(c.reply_markup is None for c in sends + edits)  # progress states have no buttons


async def test_message_not_modified_is_swallowed(notifier_env) -> None:  # noqa: ANN001
    harness, fake, notifier = notifier_env
    h = await harness.new_job()
    job = await harness.job(h.job_id)
    await notifier.progress(job, "same text")
    fake.fail_with = lambda m: (
        bad_request("Bad Request: message is not modified") if isinstance(m, EditMessageText) else None
    )
    await notifier.progress(job, "same text")
    assert len([c for c in fake.calls if isinstance(c, SendMessage)]) == 1  # no duplicate message


async def test_a_deleted_status_message_is_replaced_by_a_new_one(notifier_env) -> None:  # noqa: ANN001
    harness, fake, notifier = notifier_env
    h = await harness.new_job()
    job = await harness.job(h.job_id)
    await notifier.progress(job, "first")
    first_id = (await harness.job(h.job_id)).status_message_id
    fake.fail_with = lambda m: (
        bad_request("Bad Request: message to edit not found") if isinstance(m, EditMessageText) else None
    )
    await notifier.progress(job, "second")
    assert len([c for c in fake.calls if isinstance(c, SendMessage)]) == 2
    assert (await harness.job(h.job_id)).status_message_id != first_id  # the new id is remembered


async def test_the_notifier_uses_job_chat_id_or_the_users_telegram_id(notifier_env) -> None:  # noqa: ANN001
    harness, fake, notifier = notifier_env
    h = await harness.new_job()
    telegram_id = (await harness.user(h.user_id)).telegram_id
    await notifier.progress(await harness.job(h.job_id), "x")
    assert fake.calls[0].chat_id == telegram_id


# ---------- plan_view ----------


def test_timeline_is_numbered_in_output_time_and_accounts_for_speed() -> None:
    plan = make_plan([(0.0, 3.0), (10.0, 16.0), (20.0, 24.0)])
    plan.clips[0].role = "hook"
    plan.clips[1].speed = 2.0  # 6 s of source -> 3 s on screen
    assert timeline_lines(plan) == ["1. 0:00–0:03 🎣", "2. 0:03–0:06", "3. 0:06–0:10"]


def test_plan_message_layout_and_plain_text() -> None:
    plan = make_plan([(0.0, 4.0)], human_summary_uz="Videoni ixchamladim.")
    plan.title = "<b>Yangi</b> Malibu"  # LLM text is shown as text, never as markup
    text = plan_message(plan, ["3-qism olib tashlandi"], ["Effekt qo‘sha olmadim"])
    order = [
        text.index(x)
        for x in (
            texts.PLAN_CHANGES,
            texts.PLAN_UNSUPPORTED,
            "🎬 <b>Yangi</b> Malibu",
            "Videoni ixchamladim.",
            texts.PLAN_TIMELINE,
        )
    ]
    assert order == sorted(order)
    assert "• 3-qism olib tashlandi" in text and "• Effekt qo‘sha olmadim" in text
    assert texts.PLAN_TOTAL.format(duration="0:04") in text and texts.PLAN_WATERMARK not in text
    plain = plan_message(plan)
    assert texts.PLAN_CHANGES not in plain and texts.PLAN_UNSUPPORTED not in plain


def test_trial_plans_mention_the_watermark_and_long_plans_are_capped() -> None:
    plan = make_plan(
        [(i * 2.0, i * 2.0 + 1.0) for i in range(60)], watermark=Watermark(enabled=True, text="@bot")
    )
    text = plan_message(plan)
    assert texts.PLAN_WATERMARK in text
    assert (
        texts.PLAN_MORE_CLIPS.format(n=60 - MAX_LISTED_CLIPS) in text
        and f"{MAX_LISTED_CLIPS + 1}." not in text
    )
    assert len(plan_message(plan, ["x" * 5000])) <= TELEGRAM_TEXT_LIMIT


def test_mmss_and_callback_data_limits() -> None:
    assert [mmss(s) for s in (0, 59.6, 61, 3725)] == ["0:00", "1:00", "1:01", "62:05"]
    job_id = "12345678-1234-5678-1234-567812345678"
    for markup in (keyboards.plan_kb(job_id), keyboards.recheck_kb(job_id)):
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
    labels = [b.text for row in keyboards.plan_kb(job_id).inline_keyboard for b in row]
    assert labels == ["✅ Tasdiqlash", "✏️ O‘zgartirish", "❌ Bekor qilish"]
