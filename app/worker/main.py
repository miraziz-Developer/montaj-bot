"""arq worker entrypoint: `arq app.worker.main.WorkerSettings`."""

import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.main import build_bot
from app.bot.notifier import TelegramNotifier
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.services.ai.llm import GeminiClient
from app.services.storage import AzureBlobStorage
from app.services.stt.base import STTProvider
from app.services.stt.groq_whisper import GroqSTTProvider
from app.worker.deps import WorkerDeps
from app.worker.notifier import LoggingNotifier
from app.worker.tasks import cleanup_expired, requeue_stuck, run_analysis, run_render, run_revision

logger = logging.getLogger(__name__)

_settings = get_settings()


def build_stt(settings: Settings) -> STTProvider:
    if settings.stt_provider == "groq":
        return GroqSTTProvider(settings.groq_api_key, settings.groq_stt_model)
    raise ValueError(f"unsupported STT_PROVIDER {settings.stt_provider!r}")


async def on_startup(ctx: dict[str, Any]) -> None:
    setup_logging()
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    redis = ctx["redis"]

    async def enqueue(function_name: str, *args: Any) -> None:
        await redis.enqueue_job(function_name, *args)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    # The worker needs its OWN Bot: it edits status messages and delivers files from this process.
    bot = build_bot(settings) if settings.bot_token else None
    if bot is None:
        logger.warning("BOT_TOKEN is empty: notifications are only logged and nothing is delivered")

    ctx["engine"], ctx["bot"] = engine, bot
    ctx["deps"] = WorkerDeps(
        settings=settings,
        sessionmaker=sessionmaker,
        gemini=GeminiClient(settings),
        stt=build_stt(settings),
        storage=AzureBlobStorage(settings),
        notifier=TelegramNotifier(bot, sessionmaker) if bot else LoggingNotifier(),
        enqueue=enqueue,
        bot=bot,
    )
    try:  # ARCHITECTURE section 16: recover jobs a previous worker left half-way
        await requeue_stuck(ctx)
    except Exception:
        logger.exception("startup recovery failed")
    logger.info("worker started")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    if ctx.get("bot") is not None:
        await ctx["bot"].session.close()
    await ctx["engine"].dispose()


class WorkerSettings:
    functions = [run_analysis, run_revision, run_render, cleanup_expired]
    cron_jobs = [
        cron(cleanup_expired, minute=0),  # hourly
        cron(requeue_stuck, minute={0, 10, 20, 30, 40, 50}),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_jobs = _settings.worker_concurrency
    job_timeout = _settings.job_timeout_sec
    max_tries = 1  # retries and recovery are OUR job (fail_job / requeue_stuck_jobs), never arq's blind retry
