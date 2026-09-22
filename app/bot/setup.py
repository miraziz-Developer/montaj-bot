import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import BotCommand, ErrorEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import admin, billing, fallback, menu, plans, start
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.core.config import Settings
from app.core.errors import DomainError
from app.worker.queue import Enqueue
from app.worker.queue import enqueue as default_enqueue

logger = logging.getLogger(__name__)


def _chat_id(event: ErrorEvent) -> int | None:
    update = event.update
    if update.message is not None:
        return update.message.chat.id
    if update.callback_query is not None:
        return update.callback_query.from_user.id
    return None


async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Domain errors become their Uzbek message; anything else is logged and the update is dropped."""
    exc = event.exception
    chat_id = _chat_id(event)
    if isinstance(exc, DomainError):
        logger.info("domain error in bot handler: %s", exc.code)
        if chat_id is not None:
            await bot.send_message(chat_id, exc.message_uz)
    else:
        logger.exception("unhandled error in bot handler", exc_info=exc)
    return True


def create_dispatcher(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: BaseStorage,
    *,
    enqueue: Enqueue | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=storage)
    dp["settings"] = settings
    dp["sessionmaker"] = sessionmaker  # handlers build a TelegramNotifier from it
    dp["enqueue"] = enqueue or default_enqueue  # arq: run_render / run_revision
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dp.message.middleware(UserMiddleware())
    dp.callback_query.middleware(UserMiddleware())
    # Order matters: FSM/specific handlers first, catch-all fallback last.
    dp.include_routers(start.router, plans.router, billing.router, menu.router, admin.router, fallback.router)
    dp.errors.register(on_error)
    return dp


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command=name, description=description) for name, description in texts.COMMANDS]
