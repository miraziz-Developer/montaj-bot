from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.users import get_or_create_from_telegram

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """One AsyncSession per update in `data["session"]`: commit on success, roll back on error."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self._sessionmaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return result


class UserMiddleware(BaseMiddleware):
    """Loads/creates the DB user for messages and callbacks: `data["user"]`, `data["user_created"]`.

    Runs as an inner middleware, so updates that match no handler never create users.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = event.from_user if isinstance(event, Message | CallbackQuery) else None
        if tg_user is not None and not tg_user.is_bot:
            user, created = await get_or_create_from_telegram(
                data["session"], tg_user.id, tg_user.username, tg_user.first_name
            )
            data["user"] = user
            data["user_created"] = created
        return await handler(event, data)
