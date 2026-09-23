"""Token-light shapes sent to the LLM (RUNTIME_PROMPTS.md B.2 / C.2) and the planning context."""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.schemas.analysis import VideoAnalysis
from app.services.ai.presets import PresetRules, preset_rules_dict
from app.services.media.scenes import Scene
from app.services.media.silence import Silence
from app.services.stt.base import Transcript


@dataclass(frozen=True, slots=True)
class SourceInfo:
    duration_sec: float
    width: int
    height: int
    has_audio: bool
    has_speech: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "duration_sec": round(self.duration_sec, 2),
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "has_speech": self.has_speech,
        }


@dataclass(frozen=True, slots=True)
class BrollSourceInfo:
    """One extra B-roll video (P13): muted cutaway footage, no STT pass - just per-scene visuals."""

    source_id: str
    duration_sec: float
    width: int
    height: int
    scenes: list[dict[str, Any]] = field(default_factory=list)  # [{"scene_id","start","end","description"}]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "duration_sec": round(self.duration_sec, 2),
            "width": self.width,
            "height": self.height,
            "scenes": self.scenes,
        }


def _r(value: float) -> float:
    return round(value, 2)


def scene_dicts(scenes: Sequence[Scene]) -> list[dict[str, Any]]:
    return [{"scene_id": s.scene_id, "start": _r(s.start), "end": _r(s.end)} for s in scenes]


def transcript_per_scene(
    scenes: Sequence[Scene], transcript: Transcript, max_chars: int = 300
) -> list[dict[str, Any]]:
    """Spoken text of each scene (words overlapping the scene; falls back to overlapping segments)."""
    result = []
    for scene in scenes:
        words = transcript.words_in(scene.start, scene.end)
        if words:
            text = " ".join(w.text for w in words)
        else:
            text = " ".join(
                s.text for s in transcript.segments if s.end > scene.start and s.start < scene.end
            )
        result.append({"scene_id": scene.scene_id, "text": text[:max_chars]})
    return result


def compact_analysis(analysis: VideoAnalysis, scenes: Sequence[Scene]) -> dict[str, Any]:
    """`analysis.scenes` reduced to what the planner needs, with times taken from `scenes`."""
    times = {s.scene_id: (s.start, s.end) for s in scenes}
    compact = []
    for item in analysis.scenes:
        start, end = times.get(item.scene_id, (None, None))
        compact.append(
            {
                "scene_id": item.scene_id,
                "start": None if start is None else _r(start),
                "end": None if end is None else _r(end),
                "description": item.description,
                "highlight_score": item.highlight_score,
                "role_suggestion": item.role_suggestion,
                "usable": item.usable,
                "focus_x": item.focus_x,
                "focus_y": item.focus_y,
                "problems": item.problems,
            }
        )
    return {
        "overall": analysis.overall.model_dump(),
        "scenes": compact,
        "moments": [m.model_dump() for m in analysis.moments],
    }


def compact_transcript(
    transcript: Transcript, max_segments: int = 400, max_chars: int = 200
) -> list[dict[str, Any]]:
    """Merge consecutive segments into at most `max_segments` groups, each text cut to `max_chars`."""
    segments = transcript.segments
    if not segments:
        return []
    group = max(1, math.ceil(len(segments) / max_segments))
    merged = []
    for i in range(0, len(segments), group):
        chunk = segments[i : i + group]
        text = " ".join(s.text.strip() for s in chunk if s.text.strip())
        merged.append({"start": _r(chunk[0].start), "end": _r(chunk[-1].end), "text": text[:max_chars]})
    return merged


@dataclass(slots=True)
class PlanContext:
    """Everything the planner needs about one video (built by the worker after P06 + analysis)."""

    source: SourceInfo
    analysis: VideoAnalysis
    scenes: list[Scene]
    transcript: Transcript
    silences: list[Silence]
    music_tracks: list[dict[str, Any]] = field(default_factory=list)  # [{"id","mood"}]
    creator_profile: dict[str, str] = field(default_factory=dict)  # {"niche","purpose"}
    broll_sources: list[BrollSourceInfo] = field(default_factory=list)  # P13: optional cutaway sources

    @property
    def music_ids(self) -> set[str]:
        return {str(t["id"]) for t in self.music_tracks}

    @property
    def source_durations(self) -> dict[str, float]:
        """`{source_id: duration_sec}` for every source a `Clip.source_id` may reference (P13)."""
        durations = {"primary": self.source.duration_sec}
        durations.update({b.source_id: b.duration_sec for b in self.broll_sources})
        return durations

    def _shared(self, job: dict[str, Any], preset: PresetRules) -> dict[str, Any]:
        data = {
            "source": self.source.as_dict(),
            "job": job,
            "preset_rules": preset_rules_dict(preset),
            "transcript_segments": compact_transcript(self.transcript),
            "silences": [{"start": _r(s.start), "end": _r(s.end)} for s in self.silences],
            "music_tracks": [{"id": t["id"], "mood": t.get("mood", "")} for t in self.music_tracks],
        }
        if self.broll_sources:
            data["broll_sources"] = [b.as_dict() for b in self.broll_sources]
        return data

    def planner_input(self, job: dict[str, Any], preset: PresetRules) -> dict[str, Any]:
        """User message of the planner (B.2)."""
        data = self._shared(job, preset)
        data["creator_profile"] = self.creator_profile
        data["analysis"] = compact_analysis(self.analysis, self.scenes)
        return data

    def revision_context(self, job: dict[str, Any], preset: PresetRules) -> dict[str, Any]:
        """`context` of the revision message (C.2)."""
        data = self._shared(job, preset)
        data["analysis_compact"] = compact_analysis(self.analysis, self.scenes)
        return data
