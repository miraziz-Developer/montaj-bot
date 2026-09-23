import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum
from app.models.enums import SourceRole


class JobSource(Base):
    """One row per video attached to a job (P13 multi-source B-roll). The PRIMARY row always exists and
    matches `Job.upload_id` (kept for backward compatibility with the single-source render/notification
    code); BROLL rows are optional muted cutaway footage, capped at `settings.max_broll_sources_per_job`."""

    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("job_id", "upload_id", name="uq_job_sources_job_id_upload_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"))
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("uploads.id"))
    role: Mapped[SourceRole] = mapped_column(str_enum(SourceRole))
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
