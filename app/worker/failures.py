"""Error codes, user messages and the single place where a job is failed (with refund)."""

import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from azure.core.exceptions import AzureError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.core.errors import AIError, InvalidMedia
from app.models.enums import JobStatus
from app.models.job import Job
from app.services import billing, jobs
from app.services.media.ffmpeg import FFmpegError, redact_urls
from app.services.stt.base import STTError
from app.worker.deps import WorkerDeps

logger = logging.getLogger(__name__)

_MAX_ERROR_MESSAGE = 500
# Every non-terminal state a job can be failed from.
FAILABLE_STATES = (
    JobStatus.QUEUED,
    JobStatus.PREPROCESSING,
    JobStatus.ANALYZING,
    JobStatus.PLANNING,
    JobStatus.AWAITING_PLAN_APPROVAL,
    JobStatus.REVISING,
    JobStatus.RENDERING,
    JobStatus.DELIVERING,
)


class ErrorCode(StrEnum):
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    PROBE_FAILED = "PROBE_FAILED"
    STT_FAILED = "STT_FAILED"
    ANALYSIS_FAILED = "ANALYSIS_FAILED"
    PLANNING_FAILED = "PLANNING_FAILED"
    RENDER_FAILED = "RENDER_FAILED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    STUCK_RETRY = "STUCK_RETRY"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class Stage(StrEnum):
    PREPARE = "prepare"  # download, proxy, audio, STT, silences, scenes
    ANALYZE = "analyze"
    PLAN = "plan"
    RENDER = "render"
    DELIVER = "deliver"


_FRIENDLY = {
    ErrorCode.DOWNLOAD_FAILED: texts.FAILED_DOWNLOAD,
    ErrorCode.PROBE_FAILED: texts.FAILED_PROBE,
    ErrorCode.STT_FAILED: texts.FAILED_STT,
    ErrorCode.ANALYSIS_FAILED: texts.FAILED_ANALYSIS,
    ErrorCode.PLANNING_FAILED: texts.FAILED_PLANNING,
    ErrorCode.RENDER_FAILED: texts.FAILED_RENDER,
    ErrorCode.DELIVERY_FAILED: texts.FAILED_DELIVERY,
    ErrorCode.STUCK_RETRY: texts.FAILED_STUCK,
    ErrorCode.INTERNAL_ERROR: texts.FAILED_INTERNAL,
}
_BY_STAGE = {
    Stage.ANALYZE: ErrorCode.ANALYSIS_FAILED,
    Stage.PLAN: ErrorCode.PLANNING_FAILED,
    Stage.RENDER: ErrorCode.RENDER_FAILED,
    Stage.DELIVER: ErrorCode.DELIVERY_FAILED,
}


def friendly_uz_message_for(code: ErrorCode) -> str:
    return _FRIENDLY[code]


def classify_error(exc: BaseException, stage: Stage) -> ErrorCode:
    """Map an exception raised during `stage` to a stable error code."""
    if isinstance(exc, STTError):
        return ErrorCode.STT_FAILED
    if isinstance(exc, AIError):
        return ErrorCode.PLANNING_FAILED if stage == Stage.PLAN else ErrorCode.ANALYSIS_FAILED
    if stage == Stage.PREPARE:
        if isinstance(exc, InvalidMedia | FFmpegError):
            return ErrorCode.PROBE_FAILED
        if isinstance(exc, AzureError | OSError):
            return ErrorCode.DOWNLOAD_FAILED
        return ErrorCode.INTERNAL_ERROR
    return _BY_STAGE.get(stage, ErrorCode.INTERNAL_ERROR)


def _detail(exc: BaseException | str) -> str:
    return redact_urls(str(exc) or type(exc).__name__)[:_MAX_ERROR_MESSAGE]


async def apply_failure(session: AsyncSession, job_id: uuid.UUID, code: ErrorCode, detail: str) -> Job | None:
    """FAILED + refund inside the caller's transaction (no commit). None = the job was already terminal.

    Refund policy (ARCHITECTURE section 7): FAILED refunds everything reserved (reserve + paid revisions);
    a failed trial job gives the trial back.
    """
    failed = await jobs.transition(
        session,
        job_id,
        FAILABLE_STATES,
        JobStatus.FAILED,
        error_code=code.value,
        error_message=detail,
        finished_at=datetime.now(UTC),
    )
    if failed is None:
        return None
    await billing.refund_units(session, job_id)
    if failed.is_trial:
        await billing.release_trial(session, failed.user_id)
    return failed


async def fail_job(deps: WorkerDeps, job_id: uuid.UUID, code: ErrorCode, exc: BaseException | str) -> None:
    """FAILED + refund + user message. Never raises: a failure handler must not fail itself."""
    detail = _detail(exc)
    logger.error("job failed job_id=%s code=%s detail=%s", job_id, code, detail)
    try:
        async with deps.sessionmaker() as session:
            failed = await apply_failure(session, job_id, code, detail)
            await session.commit()
        if failed is not None:
            await deps.notifier.failed(failed, friendly_uz_message_for(code))
    except Exception:
        logger.exception("fail_job could not finish job_id=%s", job_id)
