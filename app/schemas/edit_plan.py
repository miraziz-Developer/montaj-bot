from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.render.looks import LOOKS, Look
from app.services.render.stickers import STICKERS

Aspect = Literal["9:16", "16:9", "1:1", "original"]
StylePreset = Literal["dynamic_reels", "clean_talk", "ad_commercial", "vlog_story"]
Role = Literal["hook", "body", "broll", "cta", "outro", "filler"]
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
SpeedRamp = Literal["none", "fast_to_slow", "slow_to_fast"]
RAMP_FAST = 1.6  # multiplier of clip.speed at the fast end of a ramp


def _duration_neutral_slow(fast: float) -> float:
    """The slow end s of a linear speed ramp fast<->s whose AVERAGE is 1x: ln(fast/s) / (fast - s) == 1.
    So a ramp changes the feel but never a clip's output duration (timeline, captions, dubs stay exact)."""
    lo, hi = 1e-3, 1.0 - 1e-9
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if math.log(fast / mid) / (fast - mid) > 1 else (lo, mid)
    return (lo + hi) / 2


RAMP_SLOW = _duration_neutral_slow(RAMP_FAST)  # ~0.57


def _clean_text(v: str) -> str:
    return _CTRL.sub(" ", v).strip()


class Reframe(BaseModel):
    mode: Literal["fill", "fit_blur"] = (
        "fill"  # fill = crop to fill frame; fit_blur = whole frame on blurred bg
    )
    focus_x: float = Field(0.5, ge=0.0, le=1.0)  # center of interest, 0..1 of source width
    focus_y: float = Field(0.5, ge=0.0, le=1.0)
    zoom: float = Field(1.0, ge=1.0, le=1.6)  # STATIC zoom for the whole clip (no animation in MVP)


class ClipAudio(BaseModel):
    volume: float = Field(1.0, ge=0.0, le=2.0)
    mute: bool = False
    # P13 B-roll dub: "primary" pulls audio from the job's primary source (at primary_src_in/out) instead
    # of muting a b-roll clip - the narration keeps playing while the picture cuts to the cutaway footage.
    source: Literal["own", "primary"] = "own"
    primary_src_in: float | None = Field(None, ge=0.0)
    primary_src_out: float | None = Field(None, gt=0.0)

    @model_validator(mode="after")
    def _primary_window(self) -> ClipAudio:
        if self.source == "primary":
            if self.primary_src_in is None or self.primary_src_out is None:
                raise ValueError("audio.source 'primary' needs primary_src_in and primary_src_out")
            if self.primary_src_out <= self.primary_src_in:
                raise ValueError("primary_src_out must be after primary_src_in")
        return self


class Transition(BaseModel):
    type: Literal["cut", "crossfade", "fade_black"] = "cut"  # MVP renderer treats everything as "cut"
    duration: float = Field(0.0, ge=0.0, le=1.0)


class Clip(BaseModel):
    id: str = Field(min_length=1, max_length=16)
    source_id: str = Field("primary", min_length=1, max_length=16)  # P13: "primary" or a broll_sources id
    src_in: float = Field(ge=0.0)
    src_out: float = Field(gt=0.0)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    # speed curve inside the clip (average stays `speed`); only for clips whose own sound is not heard
    speed_ramp: SpeedRamp = "none"
    role: Role = "body"
    reframe: Reframe = Field(default_factory=Reframe)
    audio: ClipAudio = Field(default_factory=ClipAudio)
    transition_in: Transition = Field(default_factory=Transition)
    note: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _range(self) -> Clip:
        if self.src_out - self.src_in < 0.3:
            raise ValueError("clip shorter than 0.3 s")
        return self

    @property
    def out_duration(self) -> float:
        return (self.src_out - self.src_in) / self.speed


class Captions(BaseModel):
    enabled: bool = True
    style: Literal["word_highlight", "pop", "karaoke", "classic"] = "word_highlight"
    font: Literal["Montserrat-Bold", "NotoSans-Bold", "Inter-Bold"] = "Montserrat-Bold"
    font_size_pct: float = Field(5.0, ge=2.5, le=9.0)  # percent of output height
    position: Literal["top", "middle", "lower_third", "bottom"] = "lower_third"
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD400"
    outline_color: str = "#000000"
    max_words_per_line: int = Field(3, ge=1, le=6)
    uppercase: bool = False

    @field_validator("primary_color", "highlight_color", "outline_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not _HEX.match(v):
            raise ValueError("color must be #RRGGBB")
        return v.upper()


class Sfx(BaseModel):
    enabled: bool = False  # whooshes on scene changes, pops on text overlays (placed by the renderer)
    volume: float = Field(0.5, ge=0.0, le=1.0)


class Music(BaseModel):
    enabled: bool = False
    track_id: str | None = None  # must exist in assets/music/catalog.json
    volume: float = Field(0.10, ge=0.0, le=0.5)  # linear gain applied to the music track
    ducking: bool = True  # lower music while someone speaks
    fade_out_sec: float = Field(2.0, ge=0.0, le=5.0)


class TextOverlay(BaseModel):
    text: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    position: Literal["top", "middle", "bottom"] = "top"
    style: Literal["title", "cta", "lower_third"] = "title"

    @field_validator("text")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = _clean_text(v)
        if not v:
            raise ValueError("empty text")
        return v

    @model_validator(mode="after")
    def _order(self) -> TextOverlay:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Sticker(BaseModel):
    emoji: str  # a name from render/stickers.py STICKERS (unknown names are dropped by EditPlan)
    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    position: Literal["top_left", "top_right", "middle_left", "middle_right"] = "top_right"
    size_pct: float = Field(16.0, ge=8.0, le=30.0)  # of the output width

    @model_validator(mode="after")
    def _order(self) -> Sticker:
        if self.end - self.start < 0.3:
            raise ValueError("sticker shorter than 0.3 s")
        return self


class Watermark(BaseModel):
    enabled: bool = False
    text: str = Field("", max_length=40)


class Export(BaseModel):
    crf: int = Field(21, ge=17, le=28)
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"] = "veryfast"
    audio_bitrate_k: int = Field(160, ge=96, le=320)
    loudnorm: bool = True
    denoise_audio: bool = False


class Target(BaseModel):
    aspect: Aspect = "9:16"
    fps: int = Field(30, ge=24, le=60)


class EditPlan(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str = Field(max_length=80)
    style_preset: StylePreset
    target: Target = Field(default_factory=Target)
    clips: list[Clip] = Field(min_length=1, max_length=400)
    captions: Captions = Field(default_factory=Captions)
    music: Music = Field(default_factory=Music)
    look: Look = "natural"
    sfx: Sfx = Field(default_factory=Sfx)
    overlays: list[TextOverlay] = Field(default_factory=list, max_length=6)
    stickers: list[Sticker] = Field(default_factory=list, max_length=8)
    watermark: Watermark = Field(default_factory=Watermark)
    export: Export = Field(default_factory=Export)
    human_summary_uz: str = Field("", max_length=1200)

    @field_validator("look", mode="before")
    @classmethod
    def _known_look(cls, v: object) -> object:
        return v if v in LOOKS else "natural"  # an unknown look name must not sink the whole plan

    @field_validator("stickers", mode="before")
    @classmethod
    def _known_stickers(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        known = [s for s in v if not isinstance(s, dict) or s.get("emoji") in STICKERS]
        return known[:8]

    @model_validator(mode="after")
    def _unique_ids(self) -> EditPlan:
        ids = [c.id for c in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("clip ids must be unique")
        return self

    def total_duration(self) -> float:
        return sum(c.out_duration for c in self.clips)
