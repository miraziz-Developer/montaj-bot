import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.enums import JobStatus

# ASSUMPTION: mirrors docs/EDIT_PLAN_SCHEMA.md; P07 should import these from schemas/edit_plan.py.
Aspect = Literal["9:16", "16:9", "1:1", "original"]
StylePreset = Literal["dynamic_reels", "clean_talk", "ad_commercial", "vlog_story"]


class TariffOut(BaseModel):
    code: str
    units: int
    price_uzs: int
    label: str
    recommended: bool


class MeOut(BaseModel):
    telegram_id: int
    first_name: str | None
    balance_units: int
    trial_available: bool
    onboarding_completed: bool
    tariffs: list[TariffOut]


class UploadInitIn(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)
    content_type: str = Field(default="", max_length=128)


class UploadInitOut(BaseModel):
    upload_id: uuid.UUID
    upload_url: str
    block_size: int
    max_parallel: int
    expires_at: datetime


class BlocksOut(BaseModel):
    uploaded_block_ids: list[str]
    block_size: int


class CompleteOut(BaseModel):
    job_id: uuid.UUID
    duration_sec: float
    width: int
    height: int
    units_cost: int
    is_trial: bool
    balance_units: int
    balance_after: int


class AttachIn(BaseModel):
    job_id: uuid.UUID


class AttachOut(BaseModel):
    upload_id: uuid.UUID
    duration_sec: float
    width: int
    height: int
    broll_count: int
    units_cost: int


class ConfirmIn(BaseModel):
    aspect: Aspect
    style_preset: StylePreset
    brief: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class ConfirmOut(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    balance_units: int


class JobPublic(BaseModel):
    """Public job view: no blob paths, no internal error text."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: JobStatus
    is_trial: bool
    aspect: str | None
    style_preset: str | None
    brief: str | None
    units_cost: int
    revision_count: int
    current_plan_version: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class JobListOut(BaseModel):
    jobs: list[JobPublic]
