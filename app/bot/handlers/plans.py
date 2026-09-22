"""Plan approval buttons (approve / revise / cancel) and the free-text revision request."""

import logging
import uuid
from collections.abc import Sequence

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.notifier import TelegramNotifier, safe_show
from app.bot.states import Revision
from app.core.errors import InvalidState
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.plan import EditPlanRow
from app.models.user import User
from app.schemas.edit_plan import EditPlan
from app.services import jobs
from app.worker.queue import Enqueue

logger = logging.getLogger(__name__)
router = Router(name="plans")
router.message.filter(F.chat.type == "private")

MAX_REVISION_CHARS = 500
_AWAITING = (JobStatus.AWAITING_PLAN_APPROVAL,)


async def require_owned_job(session: AsyncSession, job_id: uuid.UUID, telegram_user_id: int) -> Job | None:
    """The job if (and only if) it belongs to this Telegram user. Never reveals other users' jobs."""
    stmt = (
        select(Job)
        .join(User, User.id == Job.user_id)
        .where(Job.id == job_id, User.telegram_id == telegram_user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _parse_job_id(data: str | None, prefix: str) -> uuid.UUID | None:
    try:
        return uuid.UUID((data or "").removeprefix(prefix))
    except ValueError:
        return None


async def _guard(
    callback: CallbackQuery, session: AsyncSession, prefix: str, allowed: Sequence[JobStatus]
) -> Job | None:
    """Ownership + state check shared by the three buttons. Foreign/unknown job: a silent no-op."""
    job_id = _parse_job_id(callback.data, prefix)
    job = await require_owned_job(session, job_id, callback.from_user.id) if job_id else None
    if job is None:
        await callback.answer()
        return None
    if job.status not in allowed:
        await callback.answer(texts.JOB_WRONG_STATE)
        try:  # offer a refresh instead of leaving dead buttons
            await callback.message.edit_reply_markup(reply_markup=kb.recheck_kb(job.id))  # type: ignore[union-attr]
        except (TelegramAPIError, AttributeError):
            pass
        return None
    return job


@router.callback_query(F.data.startswith(kb.CB_PLAN_APPROVE))
async def plan_approve(
    callback: CallbackQuery,
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    enqueue: Enqueue,
) -> None:
    job = await _guard(callback, session, kb.CB_PLAN_APPROVE, _AWAITING)
    if job is None:
        return
    try:
        await enqueue("run_render", str(job.id))
    except Exception:
        logger.exception("could not enqueue run_render job_id=%s", job.id)
        await callback.answer(texts.ENQUEUE_FAILED, show_alert=True)
        return
    await safe_show(TelegramNotifier(callback.bot, sessionmaker), job, texts.PROGRESS_PREPARING)
    await callback.answer(texts.CB_STARTED)


@router.callback_query(F.data.startswith(kb.CB_PLAN_REVISE))
async def plan_revise(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    job = await _guard(callback, session, kb.CB_PLAN_REVISE, _AWAITING)
    if job is None:
        return
    await state.set_state(Revision.waiting_text)
    await state.update_data(job_id=str(job.id))
    await callback.answer()
    await callback.bot.send_message(callback.from_user.id, texts.ASK_REVISION)


@router.message(Revision.waiting_text, F.text, ~F.text.startswith("/"))
async def revision_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    enqueue: Enqueue,
) -> None:
    job_id = _parse_job_id((await state.get_data()).get("job_id"), "")
    job = await require_owned_job(session, job_id, message.from_user.id) if job_id else None
    await state.clear()
    if job is None or job.status not in _AWAITING:  # the state may have changed while the user typed
        await message.answer(texts.JOB_WRONG_STATE, reply_markup=kb.recheck_kb(job.id) if job else None)
        return
    try:
        await enqueue("run_revision", str(job.id), message.text[:MAX_REVISION_CHARS])
    except Exception:
        logger.exception("could not enqueue run_revision job_id=%s", job.id)
        await message.answer(texts.ENQUEUE_FAILED)
        return
    await safe_show(TelegramNotifier(message.bot, sessionmaker), job, texts.REVISION_QUEUED)


@router.callback_query(F.data.startswith(kb.CB_PLAN_CANCEL))
async def plan_cancel(
    callback: CallbackQuery,
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    user: User,
) -> None:
    job = await _guard(callback, session, kb.CB_PLAN_CANCEL, jobs.CANCELABLE_STATES)
    if job is None:
        return
    try:
        await jobs.cancel_job(session, job, user)  # the SAME function as the API's /jobs/{id}/cancel
        await session.commit()  # free the job row lock BEFORE the notifier writes to it (own session)
    except InvalidState:
        await callback.answer(texts.JOB_WRONG_STATE)
        return
    await callback.answer()
    await safe_show(TelegramNotifier(callback.bot, sessionmaker), job, texts.JOB_CANCELED)


@router.callback_query(F.data.startswith(kb.CB_PLAN_SHOW))
async def plan_show(
    callback: CallbackQuery, session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    job_id = _parse_job_id(callback.data, kb.CB_PLAN_SHOW)
    job = await require_owned_job(session, job_id, callback.from_user.id) if job_id else None
    if job is None:
        await callback.answer()
        return
    if job.status != JobStatus.AWAITING_PLAN_APPROVAL:
        await callback.answer(texts.JOB_STATUS_NOW.format(status=job.status.value), show_alert=True)
        return
    row = (
        await session.execute(
            select(EditPlanRow).where(
                EditPlanRow.job_id == job.id, EditPlanRow.version == job.current_plan_version
            )
        )
    ).scalar_one_or_none()
    await callback.answer()
    if row is not None:
        await TelegramNotifier(callback.bot, sessionmaker).plan_ready(
            job, EditPlan.model_validate(row.plan_json)
        )
