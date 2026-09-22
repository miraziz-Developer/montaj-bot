from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.services.ai.llm import LLMClient
from app.services.storage import BlobStorage
from app.services.stt.base import STTProvider
from app.worker.notifier import JobNotifier
from app.worker.queue import Enqueue


@dataclass(slots=True)
class WorkerDeps:
    """Everything a task needs; built once in `on_startup`, stored in arq's `ctx["deps"]`."""

    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    gemini: LLMClient
    stt: STTProvider
    storage: BlobStorage
    notifier: JobNotifier
    enqueue: Enqueue
    bot: Any = None  # aiogram Bot (delivery); None in tests/dev without a token
