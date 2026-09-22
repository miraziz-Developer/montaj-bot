from typing import Any

from app.models.job import Job
from app.schemas.edit_plan import EditPlan


class FakeNotifier:
    """Records every JobNotifier call as (kind, job_id, *details). `raise_on` makes a kind raise."""

    def __init__(self, raise_on: set[str] | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.raise_on = raise_on or set()

    def _record(self, kind: str, *details: Any) -> None:
        self.calls.append((kind, *details))
        if kind in self.raise_on:
            raise RuntimeError(f"notifier {kind} exploded")

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == kind]

    def texts(self, kind: str = "progress") -> list[str]:
        return [c[2] for c in self.of(kind)]

    async def plan_ready(
        self,
        job: Job,
        plan: EditPlan,
        changes_uz: list[str] | None = None,
        unsupported_uz: list[str] | None = None,
    ) -> None:
        self._record("plan_ready", job.id, plan, changes_uz, unsupported_uz)

    async def progress(self, job: Job, text_uz: str) -> None:
        self._record("progress", job.id, text_uz)

    async def failed(self, job: Job, message_uz: str) -> None:
        self._record("failed", job.id, message_uz, job.error_code)

    async def delivered(self, job: Job) -> None:
        self._record("delivered", job.id)
