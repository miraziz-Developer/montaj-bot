from collections.abc import Awaitable, Callable
from typing import Any

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings

Enqueue = Callable[..., Awaitable[Any]]


async def enqueue(function_name: str, *args: Any) -> None:
    """Enqueue an arq job by function name. Tests override `deps.get_enqueue` with a recording fake."""
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job(function_name, *args)
    finally:
        await pool.aclose()  # VERIFY: ArqRedis subclasses redis.asyncio.Redis (aclose in redis>=5)
