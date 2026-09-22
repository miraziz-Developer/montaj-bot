from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendDocument, SendMessage, SendVideo


def bad_request(text: str = "Bad Request: file is too big") -> TelegramBadRequest:
    return TelegramBadRequest(method=SendVideo(chat_id=1, video="x"), message=text)


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=SendVideo(chat_id=1, video="x"), message="connection reset")


class FakeBotClient:
    """The three Bot calls `deliver_result` uses. `*_errors` are consumed one per call (then success)."""

    def __init__(
        self,
        *,
        video_errors: list[Exception] | None = None,
        document_errors: list[Exception] | None = None,
        message_errors: list[Exception] | None = None,
    ) -> None:
        self.video_errors = list(video_errors or [])
        self.document_errors = list(document_errors or [])
        self.message_errors = list(message_errors or [])
        self.calls: list[tuple[str, int, Any, dict[str, Any]]] = []

    def _record(self, kind: str, chat_id: int, payload: Any, kwargs: dict[str, Any]) -> None:
        self.calls.append((kind, chat_id, payload, kwargs))

    def of(self, kind: str) -> list[tuple[str, int, Any, dict[str, Any]]]:
        return [c for c in self.calls if c[0] == kind]

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> Any:
        self._record("video", chat_id, video, kwargs)
        if self.video_errors:
            raise self.video_errors.pop(0)

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> Any:
        self._record("document", chat_id, document, kwargs)
        if self.document_errors:
            raise self.document_errors.pop(0)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self._record("message", chat_id, text, kwargs)
        if self.message_errors:
            raise self.message_errors.pop(0)


__all__ = ["FakeBotClient", "SendDocument", "SendMessage", "bad_request", "network_error"]
