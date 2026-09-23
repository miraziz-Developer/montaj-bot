"""Snap clip cuts to safe points (docs/EDIT_PLAN_SCHEMA.md section 4, step 3). Pure: no DB, no network."""

from collections.abc import Sequence

from app.schemas.edit_plan import Clip, EditPlan
from app.services.media.silence import Silence
from app.services.stt.base import Transcript, Word

SNAP_WINDOW_SEC = 0.35
IN_LEAD_SEC = 0.05  # src_in  -> word.start - 0.05
OUT_TAIL_SEC = 0.08  # src_out -> word.end + 0.08
MIN_CLIP_SEC = 0.3
_EPS = 1e-6


def _word_containing(t: float, words: Sequence[Word]) -> Word | None:
    return next((w for w in words if w.start + _EPS < t < w.end - _EPS), None)


def _word_edge(word: Word, kind: str) -> float:
    return max(0.0, word.start - IN_LEAD_SEC) if kind == "in" else word.end + OUT_TAIL_SEC


def snap_point(t: float, kind: str, words: Sequence[Word], silences: Sequence[Silence]) -> float:
    """Nearest safe cut position within +-0.35 s. `kind` is "in" (src_in) or "out" (src_out).

    Order: never inside a word > middle of a nearby silence gap > nearest word boundary > unchanged.
    """
    word = _word_containing(t, words)
    if word is not None:
        return _word_edge(word, kind)

    midpoints = [(s.start + s.end) / 2 for s in silences]
    near_mids = [m for m in midpoints if abs(m - t) <= SNAP_WINDOW_SEC]
    if near_mids:
        result = min(near_mids, key=lambda m: abs(m - t))
    else:
        edges = [_word_edge(w, kind) for w in words]
        near_edges = [e for e in edges if abs(e - t) <= SNAP_WINDOW_SEC]
        result = min(near_edges, key=lambda e: abs(e - t)) if near_edges else t

    landed_in = _word_containing(result, words)  # STT and silence detection can disagree slightly
    return _word_edge(landed_in, kind) if landed_in is not None else result


def _overlap(a: Clip, b: Clip) -> float:
    return min(a.src_out, b.src_out) - max(a.src_in, b.src_in)


def snap_cuts(plan: EditPlan, transcript: Transcript, silences: Sequence[Silence]) -> EditPlan:
    """A new plan whose src_in/src_out sit on safe cut points. Never yields a clip shorter than 0.3 s and
    never creates an overlap that was not already there (such clips keep their original range).

    P13: `transcript`/`silences` are timed against the PRIMARY source only, so a non-primary (B-roll)
    clip's src_in/src_out - which index into a different file entirely - are left untouched here."""
    words = transcript.all_words()
    originals = list(plan.clips)
    snapped: list[Clip] = []
    for clip in originals:
        if clip.source_id != "primary":
            snapped.append(clip)
            continue
        new_in = snap_point(clip.src_in, "in", words, silences)
        new_out = snap_point(clip.src_out, "out", words, silences)
        if new_out - new_in < MIN_CLIP_SEC:
            snapped.append(clip)
        else:
            snapped.append(clip.model_copy(update={"src_in": new_in, "src_out": new_out}))

    # Snapping moved boundaries of neighbours towards each other: undo any NEW overlap (only clips sharing
    # a source_id can meaningfully overlap - each source has its own independent timeline).
    for source_id in {c.source_id for c in snapped}:
        idxs = [i for i, c in enumerate(snapped) if c.source_id == source_id]
        order = sorted(idxs, key=lambda i: snapped[i].src_in)
        for left, right in zip(order, order[1:], strict=False):
            new_overlap = _overlap(snapped[left], snapped[right]) > 0
            old_overlap = _overlap(originals[left], originals[right]) <= 0
            if new_overlap and old_overlap:
                snapped[left], snapped[right] = originals[left], originals[right]
    return plan.model_copy(update={"clips": snapped})
