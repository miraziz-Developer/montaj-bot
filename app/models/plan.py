import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum
from app.models.enums import PlanSource


class EditPlanRow(Base):
    __tablename__ = "edit_plans"
    __table_args__ = (UniqueConstraint("job_id", "version", name="uq_edit_plans_job_id_version"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    version: Mapped[int] = mapped_column(Integer)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    human_summary: Mapped[str] = mapped_column(Text)
    source: Mapped[PlanSource] = mapped_column(str_enum(PlanSource))
    user_feedback: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
