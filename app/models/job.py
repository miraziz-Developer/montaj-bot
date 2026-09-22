import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum
from app.models.enums import JobStatus


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_user_id_status", "user_id", "status"),
        Index("ix_jobs_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("uploads.id"))
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.CREATED)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    aspect: Mapped[str | None] = mapped_column(String(16))
    style_preset: Mapped[str | None] = mapped_column(String(64))
    brief: Mapped[str | None] = mapped_column(Text)
    units_cost: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revision_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    current_plan_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status_message_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    output_blob_path: Mapped[str | None] = mapped_column(Text)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cost tracking (ARCHITECTURE section 14)
    stt_seconds: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    render_seconds: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    est_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, server_default="0")
