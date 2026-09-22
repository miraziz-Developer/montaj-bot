"""Row factories for DB tests. They flush but never commit; callers decide the transaction."""

import itertools
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, Upload, User

_counter = itertools.count(1)


async def create_user(session: AsyncSession, balance_units: int = 0, **fields: object) -> User:
    n = next(_counter)
    user = User(
        telegram_id=fields.pop("telegram_id", 1_000_000 + n),  # type: ignore[arg-type]
        referral_code=fields.pop("referral_code", f"TEST{n:04d}"),  # type: ignore[arg-type]
        balance_units=balance_units,
        **fields,
    )
    session.add(user)
    await session.flush()
    return user


async def create_job(session: AsyncSession, user_id: uuid.UUID, **fields: object) -> Job:
    upload = Upload(
        user_id=user_id,
        blob_path=f"{user_id}/{uuid.uuid4()}/source.mp4",
        original_filename="clip.mp4",
        content_type="video/mp4",
        size_bytes=1024,
    )
    session.add(upload)
    await session.flush()
    job = Job(user_id=user_id, upload_id=upload.id, **fields)
    session.add(job)
    await session.flush()
    return job
