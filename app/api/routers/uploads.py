import logging
import os
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, get_storage
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import (
    BlobMissing,
    FileTooLarge,
    InvalidMedia,
    InvalidState,
    NoCredits,
    NotFound,
    OnboardingRequired,
    SizeMismatch,
    TooLong,
    TooManyActiveJobs,
    TrialTooLong,
    UnsupportedType,
)
from app.models.enums import JobStatus, UploadStatus
from app.models.job import Job
from app.models.upload import Upload
from app.models.user import User
from app.schemas.api import BlocksOut, CompleteOut, UploadInitIn, UploadInitOut
from app.services.billing import trial_available
from app.services.jobs import count_active_jobs
from app.services.media.probe import probe
from app.services.storage import BlobStorage
from app.services.units import compute_units

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/uploads", tags=["uploads"])

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
BLOCK_SIZE = 8 * 1024 * 1024
MAX_PARALLEL = 4
PROBE_URL_TTL_HOURS = 0.25


async def _get_owned_upload(
    session: AsyncSession, user: User, upload_id: uuid.UUID, *, lock: bool = False
) -> Upload:
    stmt = select(Upload).where(Upload.id == upload_id, Upload.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update()
    upload = (await session.execute(stmt)).scalar_one_or_none()
    if upload is None:
        raise NotFound()
    return upload


async def _upload_response(upload: Upload, storage: BlobStorage, settings: Settings) -> UploadInitOut:
    url = await storage.create_upload_url(
        settings.azure_uploads_container, upload.blob_path, settings.upload_sas_ttl_min
    )
    return UploadInitOut(
        upload_id=upload.id,
        upload_url=url,
        block_size=BLOCK_SIZE,
        max_parallel=MAX_PARALLEL,
        expires_at=datetime.now(UTC) + timedelta(minutes=settings.upload_sas_ttl_min),
    )


@router.post("/init", response_model=UploadInitOut)
async def init_upload(
    body: UploadInitIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    storage: BlobStorage = Depends(get_storage),
) -> UploadInitOut:
    if not user.onboarding_completed:
        raise OnboardingRequired()
    if user.balance_units < 1 and not trial_available(user):
        raise NoCredits()
    if body.size_bytes > settings.max_upload_bytes:
        raise FileTooLarge()
    filename = body.filename.replace("\\", "/").rsplit("/", 1)[-1]
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedType()
    if await count_active_jobs(session, user.id) >= settings.max_active_jobs_per_user:
        raise TooManyActiveJobs()

    upload_id = uuid.uuid4()
    upload = Upload(
        id=upload_id,
        user_id=user.id,
        blob_path=f"{user.id}/{upload_id}/source{ext}",
        original_filename=filename[:255],
        content_type=body.content_type,
        size_bytes=body.size_bytes,
        status=UploadStatus.INIT,
    )
    session.add(upload)
    response = await _upload_response(upload, storage, settings)
    await session.commit()
    logger.info("upload init user_id=%s upload_id=%s size=%s", user.id, upload_id, body.size_bytes)
    return response


@router.post("/{upload_id}/resume", response_model=UploadInitOut)
async def resume_upload(
    upload_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    storage: BlobStorage = Depends(get_storage),
) -> UploadInitOut:
    upload = await _get_owned_upload(session, user, upload_id)
    if upload.status != UploadStatus.INIT:
        raise InvalidState()
    return await _upload_response(upload, storage, settings)


@router.get("/{upload_id}/blocks", response_model=BlocksOut)
async def uploaded_blocks(
    upload_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    storage: BlobStorage = Depends(get_storage),
) -> BlocksOut:
    upload = await _get_owned_upload(session, user, upload_id)
    if upload.status != UploadStatus.INIT:
        raise InvalidState()
    ids = await storage.list_uncommitted_block_ids(settings.azure_uploads_container, upload.blob_path)
    return BlocksOut(uploaded_block_ids=ids, block_size=BLOCK_SIZE)


def _complete_body(job: Job, upload: Upload, user: User) -> CompleteOut:
    assert upload.duration_sec is not None and upload.width is not None and upload.height is not None
    balance_after = user.balance_units if job.is_trial else user.balance_units - job.units_cost
    return CompleteOut(
        job_id=job.id,
        duration_sec=upload.duration_sec,
        width=upload.width,
        height=upload.height,
        units_cost=job.units_cost,
        is_trial=job.is_trial,
        balance_units=user.balance_units,
        balance_after=balance_after,
    )


async def _reject(session: AsyncSession, upload: Upload) -> None:
    """Persist REJECTED before the caller raises (the request session rolls back on errors)."""
    upload.status = UploadStatus.REJECTED
    await session.commit()


@router.post("/{upload_id}/complete", response_model=CompleteOut)
async def complete_upload(
    upload_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    storage: BlobStorage = Depends(get_storage),
) -> CompleteOut:
    upload = await _get_owned_upload(session, user, upload_id, lock=True)

    if upload.status == UploadStatus.VERIFIED:  # idempotent: same job again
        job = (await session.execute(select(Job).where(Job.upload_id == upload.id))).scalar_one()
        return _complete_body(job, upload, user)
    if upload.status not in (UploadStatus.INIT, UploadStatus.UPLOADED):
        raise InvalidState()

    container = settings.azure_uploads_container
    size = await storage.get_blob_size(container, upload.blob_path)
    if size is None:
        raise BlobMissing()
    if size != upload.size_bytes:
        await _reject(session, upload)
        await storage.delete_blob(container, upload.blob_path)
        raise SizeMismatch()

    read_url = await storage.create_read_url(container, upload.blob_path, PROBE_URL_TTL_HOURS, public=False)
    try:
        info = await probe(read_url)
    except InvalidMedia:
        await _reject(session, upload)
        raise
    if info.duration_sec > settings.max_video_duration_sec:
        await _reject(session, upload)
        raise TooLong()
    uses_trial = user.balance_units == 0 and trial_available(user)
    if uses_trial and info.duration_sec > settings.trial_max_duration_sec:
        await _reject(session, upload)
        raise TrialTooLong(settings.trial_max_duration_sec)

    upload.duration_sec = info.duration_sec
    upload.width = info.width
    upload.height = info.height
    upload.fps = info.fps
    upload.has_audio = info.has_audio
    upload.video_codec = info.video_codec
    upload.status = UploadStatus.VERIFIED
    upload.verified_at = datetime.now(UTC)
    # ASSUMPTION: trial jobs still record units_cost (the paid price); no units are reserved for them.
    job = Job(
        user_id=user.id,
        upload_id=upload.id,
        status=JobStatus.AWAITING_CONFIRM,
        units_cost=compute_units(info.duration_sec, settings.unit_seconds),
        is_trial=uses_trial,
    )
    session.add(job)
    await session.flush()
    body = _complete_body(job, upload, user)
    await session.commit()
    logger.info("upload verified user_id=%s upload_id=%s job_id=%s", user.id, upload.id, job.id)
    return body
