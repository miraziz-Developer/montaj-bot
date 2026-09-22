"""The real JobNotifier: talks to the user through ONE status message per job (edited in place)."""

import logging
import uuid
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import keyboards, texts
from app.bot.plan_view import plan_message
from app.models.job import Job
from app.models.user import User
from app.schemas.edit_plan import EditPlan

logger = logging.getLogger(__name__)

_NOT_MODIFIED = "message is not modified"


class TelegramNotifier:
    """Runs in the WORKER process (own Bot instance) and, for quick replies, in the bot process."""

    def __init__(self, bot: Bot, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessionmaker = sessionmaker

    async def _target(self, job: Job) -> tuple[int, int | None]:
        """(chat_id, status_message_id), read fresh: the `job` object passed in may be stale."""
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(Job.chat_id, Job.status_message_id, User.telegram_id)
                    .join(User, User.id == Job.user_id)
                    .where(Job.id == job.id)
                )
            ).one()
        return (row.chat_id or row.telegram_id), row.status_message_id

    async def _remember(self, job_id: uuid.UUID, message_id: int) -> None:
        async with self._sessionmaker() as session:
            await session.execute(update(Job).where(Job.id == job_id).values(status_message_id=message_id))
            await session.commit()

    async def show(self, job: Job, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        """Edit the job's status message; send a new one (and remember it) if there is none or it is gone."""
        chat_id, message_id = await self._target(job)
        if message_id is not None:
            try:
                await self._bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
                )
                return
            except TelegramBadRequest as exc:
                if _NOT_MODIFIED in str(exc):
                    return
                # "message to edit not found" / "can't be edited": fall through and send a fresh one
                logger.info("status message of job_id=%s cannot be edited, sending a new one", job.id)
        sent = await self._bot.send_message(chat_id, text, reply_markup=reply_markup)
        await self._remember(job.id, sent.message_id)

    async def plan_ready(
        self,
        job: Job,
        plan: EditPlan,
        changes_uz: list[str] | None = None,
        unsupported_uz: list[str] | None = None,
    ) -> None:
        await self.show(job, plan_message(plan, changes_uz, unsupported_uz), keyboards.plan_kb(job.id))

    async def progress(self, job: Job, text_uz: str) -> None:
        await self.show(job, text_uz)

    async def failed(self, job: Job, message_uz: str) -> None:
        await self.show(job, f"❌ {message_uz}\n\n{texts.FAILED_RETRY_HINT}")

    async def delivered(self, job: Job) -> None:
        """The file itself is sent by `deliver_result`; this only closes the status message."""
        await self.show(job, texts.DELIVERED_DONE)


async def safe_show(notifier: Any, job: Job, text: str) -> None:
    """For handlers: a failed edit must never break the user's action."""
    try:
        await notifier.progress(job, text)
    except TelegramAPIError:
        logger.warning("could not update the status message job_id=%s", job.id, exc_info=True)
