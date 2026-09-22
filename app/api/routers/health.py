import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_engine

logger = logging.getLogger(__name__)
router = APIRouter()


async def check_db() -> bool:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("health: database check failed")
        return False


async def check_redis() -> bool:
    client = Redis.from_url(get_settings().redis_url)
    try:
        return bool(await client.ping())
    except Exception:
        logger.exception("health: redis check failed")
        return False
    finally:
        await client.aclose()


@router.get("/health")
async def health() -> JSONResponse:
    db_ok = await check_db()
    redis_ok = await check_redis()
    ok = db_ok and redis_ok
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "db": db_ok, "redis": redis_ok},
        status_code=200 if ok else 503,
    )
