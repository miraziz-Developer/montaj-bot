"""Import every model so `Base.metadata` is complete (Alembic autogenerate, test schema setup)."""

from app.models.base import Base
from app.models.job import Job
from app.models.job_source import JobSource
from app.models.ledger import UnitLedger
from app.models.payment import Payment
from app.models.plan import EditPlanRow
from app.models.upload import Upload
from app.models.user import User

__all__ = ["Base", "EditPlanRow", "Job", "JobSource", "Payment", "UnitLedger", "Upload", "User"]
