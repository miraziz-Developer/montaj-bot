"""Sound effects on the cut points: whooshes and pops synthesised by ffmpeg itself (no sample files, so
nothing to license). The AI only switches them on (`plan.sfx`); WHERE they land is decided here.

A whoosh is pink noise that swells and snaps off, timed so its peak sits on the cut. Cues are sparse on
purpose: only real "scene changes" (B-roll in/out, role changes, fades to black) plus a jump cut when the
last cue is JUMP_CUT_GAP_SEC old, never closer than MIN_GAP_SEC."""

from dataclasses import dataclass

from app.schemas.edit_plan import EditPlan
from app.services.render.captions_ass import build_timeline

MIN_GAP_SEC = 1.4
# a plain jump cut gets a whoosh only if the last cue was this long ago: an accent, not a click track (a real
# render with 3 s put a whoosh on 7 of 9 cuts of a 36 s reel - far too busy)
JUMP_CUT_GAP_SEC = 8.0
MAX_CUES = 40
WHOOSH_SEC = 0.48
WHOOSH_PEAK_SEC = 0.32  # the swell peaks here: the cut lands on this offset
POP_SEC = 0.16
# Levels calibrated against speech normalised to -14 LUFS (ebur128): at sfx.volume 0.4 a whoosh peaks
# around -20 LUFS momentary (clearly audible, ~6 dB under the voice) and a pop around -13 dBFS true peak.
WHOOSH_GAIN = 3.5
POP_AMPLITUDE = 0.6
_STRUCTURAL_ROLES = {"hook", "cta", "outro", "broll"}


@dataclass(frozen=True, slots=True)
class SfxCue:
    t: float  # start on the OUTPUT timeline
    kind: str  # "whoosh" | "pop"


def plan_cues(plan: EditPlan) -> list[SfxCue]:
    if not plan.sfx.enabled:
        return []
    timeline = build_timeline(plan.clips)
    total = plan.total_duration()
    candidates: list[tuple[float, str, bool]] = []  # (time, kind, structural)
    for prev, entry in zip(timeline, timeline[1:], strict=False):
        a, b = prev.clip, entry.clip
        # the code's own rhythm crossfades sit on EVERY editorial cut of some styles, so they say nothing
        # about a scene change; only a fade to black, a source switch or a role change does
        structural = (
            b.transition_in.type == "fade_black"
            or a.source_id != b.source_id
            or (a.role != b.role and (a.role in _STRUCTURAL_ROLES or b.role in _STRUCTURAL_ROLES))
        )
        candidates.append((max(0.0, entry.output_start - WHOOSH_PEAK_SEC), "whoosh", structural))
    for overlay in plan.overlays:
        if overlay.start < total:
            candidates.append((overlay.start, "pop", True))
    for sticker in plan.stickers:
        if sticker.start < total:
            candidates.append((sticker.start, "pop", True))
    candidates.sort()

    cues: list[SfxCue] = []
    for t, kind, structural in candidates:
        gap = t - cues[-1].t if cues else float("inf")
        if gap < MIN_GAP_SEC or (not structural and gap < JUMP_CUT_GAP_SEC):
            continue
        cues.append(SfxCue(t, kind))
        if len(cues) >= MAX_CUES:
            break
    return cues


def _source(kind: str) -> str:
    if kind == "pop":  # a short falling blip
        return f"aevalsrc='{POP_AMPLITUDE}*sin(2*PI*(950*t-2600*t*t))*exp(-24*t)':d={POP_SEC}:s=48000"
    return (
        f"anoisesrc=d={WHOOSH_SEC}:c=pink:r=48000:a=0.9,highpass=f=250,lowpass=f=5500,"
        f"afade=t=in:st=0:d={WHOOSH_PEAK_SEC},"
        f"afade=t=out:st={WHOOSH_PEAK_SEC}:d={WHOOSH_SEC - WHOOSH_PEAK_SEC:.2f},volume={WHOOSH_GAIN}"
    )


def sfx_graph(cues: list[SfxCue], volume: float, base: str, out: str) -> str:
    """Filter-graph text that mixes the cues into the `base` audio label, producing `out`."""
    parts = []
    for i, cue in enumerate(cues):
        ms = round(cue.t * 1000)
        parts.append(
            f"{_source(cue.kind)},aformat=channel_layouts=stereo,adelay={ms}|{ms},volume={volume:g}[fx{i}]"
        )
    labels = "".join(f"[fx{i}]" for i in range(len(cues)))
    parts.append(
        f"[{base}]{labels}amix=inputs={len(cues) + 1}:duration=first:dropout_transition=0:normalize=0[{out}]"
    )
    return ";".join(parts)
