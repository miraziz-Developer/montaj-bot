"""No-LLM planner: docs/EDIT_PLAN_SCHEMA.md section 5. It must always produce a valid plan."""

from collections.abc import Sequence
from typing import Any

from app.schemas.edit_plan import Captions, Clip, EditPlan, Music, Watermark
from app.services.ai.plan_validator import DEFAULT_WATERMARK_TEXT, MIN_CLIP_SEC, apply_rhythm
from app.services.ai.presets import PresetRules
from app.services.media.silence import Silence
from app.services.stt.base import Transcript

SUMMARY_TEMPLATE = "Videodan ortiqcha pauzalarni olib tashladim va {preset_label} uslubida montaj qildim."
MIN_FRAGMENT_SEC = 1.0  # shorter kept fragments without speech are noise (cough, click)


def _keep_segments(
    duration: float, silences: Sequence[Silence], rules: PresetRules
) -> list[tuple[float, float]]:
    """Everything except silent gaps longer than remove_gap_sec (pad_sec of silence stays at each side)."""
    removals: list[tuple[float, float]] = []
    for silence in sorted(silences, key=lambda s: s.start):
        if silence.end - silence.start > rules.remove_gap_sec:
            start, end = silence.start + rules.pad_sec, silence.end - rules.pad_sec
            if end - start > 0.05:
                removals.append((max(0.0, start), min(duration, end)))
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in removals:
        if start > cursor:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        segments.append((cursor, duration))
    return [(a, b) for a, b in segments if b - a >= MIN_CLIP_SEC]


def _trim_to_target(segments: list[tuple[float, float]], max_sec: float | None) -> list[tuple[float, float]]:
    if max_sec is None:
        return segments
    kept, used = [], 0.0
    for start, end in segments:
        if used >= max_sec:
            break
        end = min(end, start + (max_sec - used))
        if end - start >= MIN_CLIP_SEC:
            kept.append((start, end))
            used += end - start
    return kept


def build_fallback_plan(
    *,
    source_duration: float,
    transcript: Transcript,
    silences: Sequence[Silence],
    preset: str,
    preset_rules: PresetRules,
    music_tracks: Sequence[dict[str, Any]],
    aspect: str,
    is_trial: bool,
    source_width: int | None = None,
    source_height: int | None = None,
) -> EditPlan:
    has_speech = bool(transcript.all_words())
    if has_speech:
        segments = [
            (a, b)
            for a, b in _keep_segments(source_duration, silences, preset_rules)
            if b - a >= MIN_FRAGMENT_SEC or transcript.words_in(a, b)
        ]
    else:
        # ASSUMPTION: without an analysis object here, "usable scenes" = the whole video.
        segments = [(0.0, source_duration)]
    segments = _trim_to_target(segments or [(0.0, source_duration)], preset_rules.target_max_sec)
    if not segments:
        segments = [(0.0, min(source_duration, preset_rules.target_max_sec or source_duration))]

    clips = [Clip(id=f"c{i}", src_in=a, src_out=b, role="body") for i, (a, b) in enumerate(segments, start=1)]
    music = Music()
    if music_tracks and preset_rules.music_volume > 0:
        music = Music(
            enabled=True, track_id=str(music_tracks[0]["id"]), volume=preset_rules.music_volume, ducking=True
        )
    plan = EditPlan(
        title=f"{preset_rules.label} montaj",
        style_preset=preset,  # type: ignore[arg-type]
        target={"aspect": aspect},  # type: ignore[arg-type]
        clips=clips,
        captions=Captions(
            enabled=has_speech,
            style=preset_rules.captions_style,
            max_words_per_line=preset_rules.captions_max_words,
            highlight_color=preset_rules.captions_highlight_color,
        ),
        music=music,
        watermark=Watermark(enabled=is_trial, text=DEFAULT_WATERMARK_TEXT if is_trial else ""),
        human_summary_uz=SUMMARY_TEMPLATE.format(preset_label=preset_rules.label),
    )
    return apply_rhythm(
        plan, transcript, preset_rules, source_width=source_width, source_height=source_height
    )
