"""Deterministic post-processing of a plan: force job settings, rhythm, validation.

docs/EDIT_PLAN_SCHEMA.md section 4 (steps 2, 4, 5). Raw LLM output never reaches the renderer without
passing through `validate_plan`.
"""

import logging
from collections.abc import Collection, Sequence

from pydantic import ValidationError

from app.schemas.edit_plan import Clip, ClipAudio, EditPlan, Transition, Watermark
from app.services.ai.presets import PresetRules
from app.services.stt.base import Transcript

logger = logging.getLogger(__name__)

MIN_CLIP_SEC = 0.3
OVERLAP_TOLERANCE_SEC = 0.05
DURATION_SLACK = 1.05
MAX_OVERLAYS = 3
MIN_SPLIT_EDGE_SEC = 1.0  # a rhythm split stays at least this far from the clip edges
MIN_PAUSE_SEC = 0.15
DEFAULT_WATERMARK_TEXT = "Montaj Bot"
_NO_ZOOM_PRESETS = {"clean_talk", "vlog_story"}


def renumber(plan: EditPlan) -> EditPlan:
    """Clip ids become c1..cN in timeline order."""
    clips = [c.model_copy(update={"id": f"c{i}"}) for i, c in enumerate(plan.clips, start=1)]
    return plan.model_copy(update={"clips": clips})


def force_job_settings(
    plan: EditPlan,
    *,
    aspect: str,
    style_preset: str,
    is_trial: bool,
    has_words: bool,
    watermark_text: str = DEFAULT_WATERMARK_TEXT,
) -> EditPlan:
    """The job, not the LLM, decides aspect, style, watermark and whether captions can exist."""
    watermark = Watermark(enabled=True, text=watermark_text[:40]) if is_trial else Watermark()
    captions = plan.captions if has_words else plan.captions.model_copy(update={"enabled": False})
    return plan.model_copy(
        update={
            "target": plan.target.model_copy(update={"aspect": aspect}),
            "style_preset": style_preset,
            "watermark": watermark,
            "captions": captions,
        }
    )


# ---------- rhythm ----------


def _split_candidates(transcript: Transcript, lo: float, hi: float) -> list[tuple[float, float]]:
    """(time, score) of pauses/sentence ends inside [lo, hi]; the cut sits in the middle of the gap."""
    found: list[tuple[float, float]] = []
    for prev, nxt in zip(transcript.segments, transcript.segments[1:], strict=False):
        t = (prev.end + nxt.start) / 2
        if lo <= t <= hi:
            found.append((t, 1.0 + max(0.0, nxt.start - prev.end)))
    words = transcript.all_words()
    for prev, nxt in zip(words, words[1:], strict=False):
        gap = nxt.start - prev.end
        t = (prev.end + nxt.start) / 2
        if gap >= MIN_PAUSE_SEC and lo <= t <= hi:
            found.append((t, min(gap, 1.0)))
    return found


def _out_of_word(t: float, transcript: Transcript, lo: float, hi: float) -> float:
    for word in transcript.all_words():
        if word.start < t < word.end:
            if lo <= word.end <= hi:
                return word.end
            if lo <= word.start <= hi:
                return word.start
    return t


def _split_clip(clip: Clip, transcript: Transcript, max_shot_sec: float) -> list[tuple[float, float]]:
    max_src = max_shot_sec * clip.speed
    ranges: list[tuple[float, float]] = []
    cursor = clip.src_in
    while clip.src_out - cursor > max_src + 1e-6:
        target = cursor + max_src
        lo, hi = cursor + MIN_SPLIT_EDGE_SEC, min(target, clip.src_out - MIN_SPLIT_EDGE_SEC)
        candidates = _split_candidates(transcript, lo, hi)
        if candidates:
            cut = max(candidates, key=lambda c: c[1] - 0.1 * abs(c[0] - target))[0]
        else:
            cut = _out_of_word(min(max(target, lo), hi), transcript, lo, hi)
        ranges.append((cursor, cut))
        cursor = cut
    ranges.append((cursor, clip.src_out))
    return ranges


def _piece_role(original: str, index: int, count: int) -> str:
    if original == "hook":
        return "hook" if index == 0 else "body"
    if original in ("cta", "outro"):
        return original if index == count - 1 else "body"
    return original


def _looks_like_a_round_video_note(width: int | None, height: int | None) -> bool:
    """Telegram "video note" round messages (and similar saved/reposted clips) are always exactly square
    and small, with the real content circle-masked - black corners baked into the pixels. `fill` mode's
    center crop then keeps the FULL height of that circle, showing mostly black near the top/bottom of the
    frame (the circle is narrow there) - unlike a normal square PHOTO/video, which has real content in its
    corners too and crops cleanly. A small square is the only cheap, reliable signal available here (no
    frame pixel analysis): genuine square footage from a phone or a proper camera is essentially never this
    small, but a video note commonly is (Telegram renders these around 240-640px)."""
    if not width or not height:
        return False
    is_square = abs(width - height) / max(width, height) < 0.02
    is_small = min(width, height) <= 640
    return is_square and is_small


def apply_rhythm(
    plan: EditPlan,
    transcript: Transcript,
    preset: PresetRules,
    *,
    source_width: int | None = None,
    source_height: int | None = None,
) -> EditPlan:
    """Split clips longer than `max_shot_sec`, alternate zoom levels, renumber ids."""
    clips: list[Clip] = []
    for clip in plan.clips:
        # B-roll src_in/src_out index into their OWN file, not the primary transcript's timeline - the
        # pause-based split logic below only makes sense for the primary source.
        ranges = (
            _split_clip(clip, transcript, preset.max_shot_sec)
            if clip.source_id == "primary"
            else [(clip.src_in, clip.src_out)]
        )
        for i, (start, end) in enumerate(ranges):
            clips.append(
                clip.model_copy(
                    update={
                        "src_in": start,
                        "src_out": end,
                        "role": _piece_role(clip.role, i, len(ranges)),
                        "note": clip.note if i == 0 else None,
                    }
                )
            )
    if _looks_like_a_round_video_note(source_width, source_height):
        # Overrides the AI's reframe choice entirely - like zoom_levels/crossfade_sec, this is a code
        # decision, not something the model is well-positioned to judge from a single analyzed frame.
        for i, clip in enumerate(clips):
            reframe = clip.reframe.model_copy(update={"mode": "fit_blur"})
            clips[i] = clip.model_copy(update={"reframe": reframe})
    elif not (plan.target.aspect == "16:9" and preset.key in _NO_ZOOM_PRESETS):
        levels = preset.zoom_levels
        for i, clip in enumerate(clips):
            reframe = clip.reframe.model_copy(update={"zoom": levels[i % len(levels)]})
            clips[i] = clip.model_copy(update={"reframe": reframe})
    if preset.crossfade_sec > 0:
        # Every cut gets the same short crossfade (a per-style constant, not an AI choice - like zoom_levels
        # above); clip 0 keeps its default transition_in since nothing precedes it.
        transition = Transition(type="crossfade", duration=preset.crossfade_sec)
        for i, clip in enumerate(clips):
            if i > 0:
                clips[i] = clip.model_copy(update={"transition_in": transition})
    return renumber(plan.model_copy(update={"clips": clips}))


# ---------- validation ----------


def _total(clips: Sequence[Clip]) -> float:
    return sum(c.out_duration for c in clips)


def _fix_or_drop_primary_dub(clip: Clip, primary_duration: float) -> Clip:
    """A B-roll clip's audio.source=="primary" window must lie inside the primary source's own duration;
    an AI mistake here falls back to muting that clip rather than failing the whole plan."""
    audio = clip.audio
    if audio.source != "primary":
        return clip
    lo = max(0.0, audio.primary_src_in or 0.0)
    hi = min(primary_duration, audio.primary_src_out or 0.0)
    if hi - lo < MIN_CLIP_SEC:
        return clip.model_copy(update={"audio": ClipAudio(volume=audio.volume, mute=True)})
    if (lo, hi) != (audio.primary_src_in, audio.primary_src_out):
        audio = audio.model_copy(update={"primary_src_in": lo, "primary_src_out": hi})
        clip = clip.model_copy(update={"audio": audio})
    return clip


def validate_plan(
    plan: EditPlan,
    source_duration: float,
    preset: PresetRules,
    music_ids: Collection[str],
    broll_durations: dict[str, float] | None = None,
) -> tuple[EditPlan, list[str]]:
    """Auto-fix soft issues; return (plan, hard_errors). A non-empty error list means: use the fallback.
    `broll_durations` maps a B-roll `Clip.source_id` to that source's own duration (P13); a clip whose
    source_id isn't "primary" and isn't in this map is rejected (the AI referenced an unknown source)."""
    errors: list[str] = []
    broll_durations = broll_durations or {}

    clips: list[Clip] = []
    for clip in plan.clips:
        limit = source_duration if clip.source_id == "primary" else broll_durations.get(clip.source_id)
        if limit is None:
            errors.append(f"clip {clip.id} references unknown source_id {clip.source_id!r}")
            continue
        if clip.src_in >= limit or clip.src_out <= 0:
            errors.append(f"clip {clip.id} lies entirely outside its source (0..{limit:.2f}s)")
            continue
        start, end = max(0.0, clip.src_in), min(limit, clip.src_out)
        if end - start < MIN_CLIP_SEC:
            continue  # sliver left after clamping: drop
        if (start, end) != (clip.src_in, clip.src_out):
            clip = clip.model_copy(update={"src_in": start, "src_out": end})
        clips.append(_fix_or_drop_primary_dub(clip, source_duration))

    if preset.keep_chronology and all(c.source_id == "primary" for c in clips):
        clips.sort(key=lambda c: c.src_in)  # mixed sources have no single shared timeline to sort by

    if not clips:
        errors.append("plan has no usable clips")
    for source_id in {c.source_id for c in clips}:
        same_source = sorted((c for c in clips if c.source_id == source_id), key=lambda c: c.src_in)
        for a, b in zip(same_source, same_source[1:], strict=False):
            overlap = a.src_out - b.src_in
            if overlap > OVERLAP_TOLERANCE_SEC:
                errors.append(f"clips {a.id} and {b.id} overlap in source {source_id!r} by {overlap:.2f}s")

    total = _total(clips)
    all_footage = source_duration + sum(broll_durations.values())
    if total > all_footage * DURATION_SLACK:
        errors.append(f"plan is longer ({total:.1f}s) than the available footage ({all_footage:.1f}s)")

    music = plan.music
    if music.enabled and (music.track_id is None or music.track_id not in music_ids):
        music = music.model_copy(update={"enabled": False, "track_id": None})

    overlays = []
    for overlay in plan.overlays:  # drop unusable ones first, then cap
        if overlay.start >= total - 0.2:
            continue
        end = min(overlay.end, total)
        if end - overlay.start >= 0.2:
            overlays.append(overlay.model_copy(update={"end": end}))
    overlays = overlays[:MAX_OVERLAYS]

    fixed = renumber(plan.model_copy(update={"clips": clips, "music": music, "overlays": overlays}))
    if errors:
        return fixed, errors
    try:
        return EditPlan.model_validate(fixed.model_dump()), []
    except ValidationError as exc:
        return fixed, [f"plan failed schema validation: {exc.errors()[0]['msg']}"]
