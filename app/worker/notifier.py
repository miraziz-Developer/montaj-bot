import logging
from typing import Protocol

from app.models.job import Job
from app.schemas.edit_plan import EditPlan

logger = logging.getLogger(__name__)


class JobNotifier(Protocol):
    """How the worker talks to the user. The Telegram implementation arrives in P10."""

    async def plan_ready(
        self,
        job: Job,
        plan: EditPlan,
        changes_uz: list[str] | None = None,
        unsupported_uz: list[str] | None = None,
    ) -> None: ...

    async def progress(self, job: Job, text_uz: str) -> None: ...

    async def failed(self, job: Job, message_uz: str) -> None: ...

    async def delivered(self, job: Job) -> None: ...


class LoggingNotifier:
    """Placeholder used until the Telegram notifier exists: logs what would have been sent."""

    async def plan_ready(
        self,
        job: Job,
        plan: EditPlan,
        changes_uz: list[str] | None = None,
        unsupported_uz: list[str] | None = None,
    ) -> None:
        logger.info("notify plan_ready job_id=%s clips=%s", job.id, len(plan.clips))

    async def progress(self, job: Job, text_uz: str) -> None:
        logger.info("notify progress job_id=%s text=%s", job.id, text_uz)

    async def failed(self, job: Job, message_uz: str) -> None:
        logger.info("notify failed job_id=%s error_code=%s", job.id, job.error_code)

    async def delivered(self, job: Job) -> None:
        logger.info("notify delivered job_id=%s", job.id)
