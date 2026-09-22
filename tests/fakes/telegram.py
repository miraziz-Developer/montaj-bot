"""A fake aiogram session: records every Bot API call and returns plausible results (no network)."""

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        # optional hook: return an exception to make that Bot API call fail
        self.fail_with: Callable[[TelegramMethod[Any]], Exception | None] | None = None

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        if self.fail_with is not None and (error := self.fail_with(method)) is not None:
            raise error
        if method.__returning__ is Message:
            chat_id = int(getattr(method, "chat_id", 0))
            return Message(
                message_id=len(self.calls),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    # ---- helpers for assertions ----

    @property
    def sent(self) -> list[SendMessage]:
        return [call for call in self.calls if isinstance(call, SendMessage)]

    def sent_to(self, chat_id: int) -> list[SendMessage]:
        return [call for call in self.sent if call.chat_id == chat_id]

    def texts_to(self, chat_id: int) -> list[str]:
        return [call.text for call in self.sent_to(chat_id)]

    def clear(self) -> None:
        self.calls.clear()
