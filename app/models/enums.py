from enum import StrEnum


class JobStatus(StrEnum):
    CREATED = "CREATED"
    AWAITING_CONFIRM = "AWAITING_CONFIRM"
    QUEUED = "QUEUED"
    PREPROCESSING = "PREPROCESSING"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    REVISING = "REVISING"
    RENDERING = "RENDERING"
    DELIVERING = "DELIVERING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


class UploadStatus(StrEnum):
    INIT = "INIT"
    UPLOADED = "UPLOADED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class LedgerReason(StrEnum):
    PURCHASE = "purchase"
    RESERVE = "reserve"
    REFUND = "refund"
    REVISION = "revision"
    REFERRAL = "referral"
    ADMIN_GRANT = "admin_grant"


class PlanSource(StrEnum):
    AI_INITIAL = "ai_initial"
    AI_REVISION = "ai_revision"
    FALLBACK = "fallback"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PaymentProvider(StrEnum):
    MANUAL = "manual"
    PAYME = "payme"
    CLICK = "click"
