"""Deliver the finished video: chat (video, then document) or a 48 h download link; then DONE."""

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import FSInputFile
from sqlalchemy import select

from app.bot import texts
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.user import User
from app.services import jobs
from app.services.referrals import reward_referrer
from app.worker.deps import WorkerDeps

logger = logging.getLogger(__name__)

CLOUD_API_FILE_LIMIT = 50_000_000  # the public Bot API cannot upload more than this
SEND_ATTEMPTS = 3
_sleep = asyncio.sleep  # tests replace this (never patch asyncio.sleep globally)
LINK_TTL_HOURS = 48


class DeliveryBot(Protocol):
    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> Any: ...

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> Any: ...

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any: ...


def max_inline_bytes(deps: WorkerDeps) -> int:
    """Inline limit; without a local Bot API server (TELEGRAM_API_BASE) files above 50 MB go as a link."""
    limit = deps.settings.deliver_max_inline_bytes
    return limit if deps.settings.telegram_api_base else min(limit, CLOUD_API_FILE_LIMIT)


async def _send_with_retries(send: Any) -> None:
    """Network hiccups are retried (ARCHITECTURE section 16); a bad request is not (fall back instead)."""
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            await send()
            return
        except TelegramNetworkError:
            if attempt == SEND_ATTEMPTS:
                raise
            await _sleep(2 ** (attempt - 1))


async def _send_file(bot: DeliveryBot, chat_id: int, path: Path) -> bool:
    """True if the video reached the chat as a video or as a document."""
    for method, kwargs in (
        (bot.send_video, {"video": FSInputFile(path)}),
        (bot.send_document, {"document": FSInputFile(path)}),
    ):
        try:
            await _send_with_retries(
                lambda method=method, kwargs=kwargs: method(chat_id, caption=texts.DELIVERY_CAPTION, **kwargs)
            )
            return True
        except TelegramAPIError as exc:
            logger.warning("delivery via %s failed: %s", method.__name__, type(exc).__name__)
    return False


async def deliver_result(deps: WorkerDeps, job: Job) -> None:
    """Send the result, then DELIVERING -> DONE (+ referral reward) and close the status message.

    Without a bot (no token: dev/tests) only the notifier's `delivered` runs. If every delivery path
    fails this raises, and the caller fails the job (DELIVERY_FAILED + refund).
    """
    if deps.bot is not None and job.output_blob_path:
        await _send_result(deps, deps.bot, job)

    async with deps.sessionmaker() as session:
        done = await jobs.transition(
            session,
            job.id,
            [JobStatus.DELIVERING],
            JobStatus.DONE,
            finished_at=datetime.now(UTC),
            error_code=None,
        )
        if done is not None:
            await reward_referrer(session, done)
        await session.commit()
    try:
        await deps.notifier.delivered(done or job)
    except Exception:
        logger.warning("could not close the status message job_id=%s", job.id, exc_info=True)


async def _send_result(deps: WorkerDeps, bot: DeliveryBot, job: Job) -> None:
    settings = deps.settings
    chat_id = job.chat_id or await _telegram_id(deps, job)
    outputs, blob = settings.azure_outputs_container, job.output_blob_path
    assert blob is not None

    if (job.output_size_bytes or 0) <= max_inline_bytes(deps):
        workdir = Path(settings.tmp_dir) / f"{job.id}-deliver"
        try:
            path = workdir / "final.mp4"
            await deps.storage.download_to_file(outputs, blob, path)
            if await _send_file(bot, chat_id, path):
                return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    url = await deps.storage.create_read_url(outputs, blob, ttl_hours=LINK_TTL_HOURS)
    await bot.send_message(chat_id, texts.DELIVERY_LINK.format(url=url))  # raising here = DELIVERY_FAILED


async def _telegram_id(deps: WorkerDeps, job: Job) -> int:
    async with deps.sessionmaker() as session:
        return int(await session.scalar(select(User.telegram_id).where(User.id == job.user_id)))
