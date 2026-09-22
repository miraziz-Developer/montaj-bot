from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class OverallInfo(BaseModel):
    summary: str = Field("", max_length=400)
    topic: str = Field("", max_length=120)
    language: str = Field("unknown", max_length=40)
    tone: str = Field("neutral", max_length=60)
    has_speech: bool = False
    main_subject: str = Field("", max_length=120)


class SceneQuality(BaseModel):
    sharpness: float = Field(0.5, ge=0.0, le=1.0)
    exposure: float = Field(0.5, ge=0.0, le=1.0)
    stability: float = Field(0.5, ge=0.0, le=1.0)
    audio_clarity: float = Field(0.5, ge=0.0, le=1.0)


class SceneAnalysis(BaseModel):
    scene_id: int
    description: str = Field("", max_length=240)
    shot_type: Literal["closeup", "medium", "wide", "detail", "screen", "other"] = "other"
    subjects: list[str] = Field(default_factory=list, max_length=6)
    on_screen_text: list[str] = Field(default_factory=list, max_length=6)
    motion: Literal["low", "medium", "high"] = "medium"
    quality: SceneQuality = Field(default_factory=SceneQuality)
    emotion: str = Field("neutral", max_length=40)
    highlight_score: float = Field(0.3, ge=0.0, le=1.0)
    role_suggestion: Literal["hook", "body", "broll", "cta", "outro", "filler"] = "body"
    usable: bool = True
    problems: list[str] = Field(default_factory=list, max_length=6)
    focus_x: float = Field(0.5, ge=0.0, le=1.0)
    focus_y: float = Field(0.5, ge=0.0, le=1.0)


class KeyMoment(BaseModel):
    scene_id: int
    type: Literal["hook_candidate", "punchline", "product_reveal", "cta", "mistake", "laugh", "dead_air"]
    note: str = Field("", max_length=160)


class VideoAnalysis(BaseModel):
    overall: OverallInfo = Field(default_factory=OverallInfo)
    scenes: list[SceneAnalysis]
    moments: list[KeyMoment] = Field(default_factory=list)
