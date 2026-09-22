import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum
from app.models.enums import LedgerReason


class UnitLedger(Base):
    """Append-only. Invariant: users.balance_units == SUM(delta) per user. Never UPDATE/DELETE rows."""

    __tablename__ = "unit_ledger"
    __table_args__ = (
        CheckConstraint("delta <> 0", name="delta_non_zero"),
        Index("ix_unit_ledger_user_id_created_at", "user_id", "created_at"),
        Index("ix_unit_ledger_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[LedgerReason] = mapped_column(str_enum(LedgerReason))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id"))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
