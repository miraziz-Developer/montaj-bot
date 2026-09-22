from functools import lru_cache

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import Unauthorized
from app.core.security import verify_init_data
from app.models.user import User
from app.services.storage import AzureBlobStorage, BlobStorage
from app.services.users import get_or_create_from_telegram
from app.worker.queue import Enqueue, enqueue


@lru_cache
def get_storage() -> BlobStorage:
    return AzureBlobStorage(get_settings())


def get_enqueue() -> Enqueue:
    return enqueue


async def current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    """Authenticate `Authorization: tma <initData>` and return the DB user (created on first sight)."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "tma" or not token.strip():
        raise Unauthorized()
    tg_user = verify_init_data(token.strip(), settings.bot_token)
    user, _ = await get_or_create_from_telegram(session, tg_user.id, tg_user.username, tg_user.first_name)
    await session.commit()
    return user
