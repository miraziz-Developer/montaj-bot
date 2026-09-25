"""Face-aware framing: a virtual camera operator for `fill` reframes (as in ClipsAI, OpenShorts, AutoClip).

The planner's `focus_x/focus_y` come from ONE analysed frame, so a speaker who walks or leans out of that spot
drifts out of a 9:16 crop of a 16:9 source. Here the render samples the clip's frames, finds faces with YuNet
(OpenCV, MIT, ~0.2 MB, CPU), follows one subject, and turns the raw detections into a smooth camera path:
gaps are filled, a dead zone keeps the framing still while the subject stays near the centre, and movement is
eased and speed-capped so it never whips. The path is returned as a few keypoints that `clip_stage` writes
into ffmpeg's `crop` x/y expressions (piecewise-linear in `t`).

Tracking is an enhancement, never a requirement: any failure (no model, no OpenCV, no face) returns `[]`
and the render falls back to the planner's static focus.
"""

import asyncio
import logging
import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 4.0
MAX_SAMPLES = 240  # bounds memory and time for long clips
ANALYSIS_WIDTH = 480
SCORE_THRESHOLD = 0.6
MIN_DETECTED_FRACTION = 0.35  # fewer frames with a face than this: no reliable subject, keep the static focus
MAX_INTERPOLATED_GAP_SEC = 1.5  # a longer gap is "subject left": hold the last position instead of drifting
STALE_SUBJECT_SEC = 1.5  # after this long without a face the next detection starts a fresh subject choice
# fraction of the crop size the subject may wander from the crop centre before the camera moves
DEAD_ZONE = 0.15
EASE_TAU_SEC = 0.35  # smoothing time constant of the camera
MAX_SPEED = 0.5  # crop sizes per second: the fastest the camera is allowed to pan
KEY_INTERVAL_SEC = 0.5
FULL_SPAN = 0.98  # a crop spanning this much of the frame on an axis has nothing to follow on that axis
FFMPEG_TIMEOUT_SEC = 120
# active speaker: with several faces the camera follows the one whose mouth moves (lightweight audio-free
# diarization); a new speaker must out-talk the current one by SWITCH_RATIO for SWITCH_HOLD_SEC
TRACK_MATCH_DIST = 0.12  # a face within this (frame widths) of a track's last position continues that track
ACTIVITY_TAU_SEC = 0.8
SWITCH_RATIO = 1.6
SWITCH_MIN_ACTIVITY = 0.08
SWITCH_HOLD_SEC = 0.75
CUT_DISTANCE = 0.5  # a speaker switch farther than this (crop widths) is a hard cut, nearer ones are panned
CUT_EDGE_SEC = 0.02
MOUTH_PATCH = (24, 12)
MOUTH_MEMORY_SAMPLES = 6  # a face missed by the detector keeps its last mouth patch this long


@dataclass(frozen=True, slots=True)
class Face:
    cx: float  # centre, 0..1 of frame width
    cy: float
    w: float  # size, 0..1 of frame width/height
    h: float
    score: float
    mouth: float = 0.0  # mouth-shape change since the previous sample (0 = still); see _mouth_activity


@dataclass(frozen=True, slots=True)
class FocusKey:
    t: float  # seconds from the clip's start, in SOURCE time (the crop filter runs before speed changes)
    x: float  # 0..1 of the frame width
    y: float  # 0..1 of the frame height


# ---------- pure logic (unit-tested without OpenCV or ffmpeg) ----------


@dataclass(slots=True)
class _Track:
    face: Face
    last_t: float
    activity: float = 0.0
    raw: dict[int, float] = field(default_factory=dict)  # sample index -> mouth activity


def pick_speaker(
    frames: Sequence[Sequence[Face]], times: Sequence[float]
) -> tuple[list[Face | None], set[int]]:
    """One face per sample, plus the sample indices where the camera should CUT to a new speaker.

    Faces are linked into tracks by proximity. The subject starts as the largest face and stays until
    another track's mouth activity (EMA) beats it by SWITCH_RATIO for SWITCH_HOLD_SEC - then the subject
    becomes that speaker, backdated to when they started out-talking. Without mouth data (all 0) this is
    "largest first, then stay on it", so a bystander crossing the shot never steals the camera."""
    tracks: list[_Track] = []
    subject: _Track | None = None
    challenger: tuple[_Track, int] | None = None  # (track, sample index where it started out-talking)
    chosen: list[Face | None] = []
    switches: set[int] = set()
    prev_t = times[0] if times else 0.0
    for i, (faces, t) in enumerate(zip(frames, times, strict=True)):
        decay = math.exp(-(t - prev_t) / ACTIVITY_TAU_SEC)
        prev_t = t
        live = [tr for tr in tracks if t - tr.last_t <= STALE_SUBJECT_SEC]
        seen: list[_Track] = []
        for face in sorted(faces, key=lambda f: -f.w * f.h):
            match = min(
                (tr for tr in live if tr not in seen),
                key=lambda tr: math.hypot(tr.face.cx - face.cx, tr.face.cy - face.cy),
                default=None,
            )
            if (
                match is None
                or math.hypot(match.face.cx - face.cx, match.face.cy - face.cy) > TRACK_MATCH_DIST
            ):
                match = _Track(face, t, face.mouth)
                tracks.append(match)
            else:
                match.activity = match.activity * decay + face.mouth * (1 - decay)
                match.face, match.last_t = face, t
            match.raw[i] = face.mouth
            seen.append(match)
        if subject is not None and subject not in seen and t - subject.last_t > STALE_SUBJECT_SEC:
            subject, challenger = None, None
        if subject is None and seen:
            subject = seen[0]  # largest visible face
        if subject is not None and len(seen) > 1:
            louder = max((tr for tr in seen if tr is not subject), key=lambda tr: tr.activity)
            beats = louder.activity > max(SWITCH_MIN_ACTIVITY, SWITCH_RATIO * subject.activity)
            if not beats:
                challenger = None
            elif challenger is None or challenger[0] is not louder:
                challenger = (louder, i)
            elif t - times[challenger[1]] >= SWITCH_HOLD_SEC:
                start = _speech_onset(louder, subject, challenger[1], times)
                subject = louder
                challenger = None
                switches.add(start)
                for j in range(start, i):  # the new speaker was already talking: reframe from then
                    chosen[j] = louder.face if chosen[j] is not None else None
        chosen.append(subject.face if subject is not None and subject in seen else None)
    return chosen, switches


def _speech_onset(new: _Track, old: _Track, start: int, times: Sequence[float]) -> int:
    """The smoothed activity crosses over late (EMA lag): walk back while the new speaker's RAW mouth
    activity still beats the old one's, at most 2*ACTIVITY_TAU_SEC, so the cut lands when they began.
    Compared over 2-sample windows: a talking mouth is momentarily still between syllables."""

    def recent(track: _Track, j: int) -> float:
        return (track.raw.get(j, 0.0) + track.raw.get(j + 1, 0.0)) / 2  # sample j and the one after

    j = start
    while (
        j > 0
        and times[start] - times[j - 1] <= 2 * ACTIVITY_TAU_SEC
        and recent(new, j - 1) > max(SWITCH_MIN_ACTIVITY, recent(old, j - 1))
    ):
        j -= 1
    return j


def pick_subject(frames: Sequence[Sequence[Face]], times: Sequence[float]) -> list[Face | None]:
    return pick_speaker(frames, times)[0]


def _fill_gaps(times: Sequence[float], values: Sequence[float | None]) -> list[float]:
    known = [i for i, v in enumerate(values) if v is not None]
    filled: list[float] = []
    for i, t in enumerate(times):
        value = values[i]
        if value is not None:
            filled.append(float(value))
            continue
        before = max((k for k in known if k < i), default=None)
        after = min((k for k in known if k > i), default=None)
        before_v = None if before is None else values[before]
        after_v = None if after is None else values[after]
        if before is None or before_v is None:
            filled.append(float(after_v if after_v is not None else 0.5))
        elif after is None or after_v is None:
            filled.append(float(before_v))
        elif times[after] - times[before] <= MAX_INTERPOLATED_GAP_SEC:
            share = (t - times[before]) / (times[after] - times[before])
            filled.append(float(before_v) * (1 - share) + float(after_v) * share)
        else:
            filled.append(float(before_v))
    return filled


def _camera(
    times: Sequence[float], target: Sequence[float], crop: float, cuts: Collection[int] = ()
) -> list[float]:
    """Follow `target` with a dead zone, exponential easing and a speed cap. `crop` = the crop's size on this
    axis as a fraction of the frame; a crop spanning the whole axis cannot move, so it is held at the mean."""
    if crop >= FULL_SPAN:
        mean = sum(target) / len(target)
        return [mean] * len(target)
    dead = DEAD_ZONE * crop
    half = crop / 2
    cam = min(max(target[0], half), 1 - half)
    path = [cam]
    for i in range(1, len(times)):
        dt = max(times[i] - times[i - 1], 1e-6)
        error = target[i] - cam
        if i in cuts:  # a speaker switch: cut straight to them, like an editor would
            cam = target[i]
        elif abs(error) > dead:
            goal = target[i] - math.copysign(dead, error)
            step = (goal - cam) * (1 - math.exp(-dt / EASE_TAU_SEC))
            limit = MAX_SPEED * crop * dt
            cam += max(-limit, min(limit, step))
        cam = min(max(cam, half), 1 - half)
        path.append(cam)
    return path


def build_path(
    times: Sequence[float],
    subject: Sequence[Face | None],
    duration: float,
    *,
    crop_w: float,
    crop_h: float,
    switches: Collection[int] = (),
) -> list[FocusKey]:
    """Raw per-sample detections -> keypoints of a smooth camera path (empty = no reliable subject).
    `switches` (from pick_speaker) far enough apart become hard cuts: two keys CUT_EDGE_SEC apart."""
    if not times or duration <= 0:
        return []
    detected = sum(1 for f in subject if f is not None)
    if detected / len(subject) < MIN_DETECTED_FRACTION:
        return []
    tx = _fill_gaps(times, [f.cx if f else None for f in subject])
    ty = _fill_gaps(times, [f.cy if f else None for f in subject])
    cuts = {i for i in switches if 0 < i < len(times) and abs(tx[i] - tx[i - 1]) > CUT_DISTANCE * crop_w}
    xs = _camera(times, tx, crop_w, cuts)
    ys = _camera(times, ty, crop_h, cuts if crop_h < FULL_SPAN else ())

    def at(series: Sequence[float], t: float, lo: int, hi: int) -> float:
        """Linear interpolation over samples lo..hi-1 only (never across a cut); held outside them."""
        if t <= times[lo]:
            return series[lo]
        for i in range(lo + 1, hi):
            if t <= times[i]:
                share = (t - times[i - 1]) / max(times[i] - times[i - 1], 1e-6)
                return series[i - 1] * (1 - share) + series[i] * share
        return series[hi - 1]

    bounds = [0, *sorted(cuts), len(times)]
    keys: list[FocusKey] = []
    for seg, (lo, hi) in enumerate(zip(bounds, bounds[1:], strict=False)):
        start = 0.0 if seg == 0 else times[lo]
        end = duration if hi == len(times) else times[hi] - CUT_EDGE_SEC
        marks = [start]
        while marks[-1] + KEY_INTERVAL_SEC < end - 1e-6:
            marks.append(marks[-1] + KEY_INTERVAL_SEC)
        if end > marks[-1] + 1e-6:
            marks.append(end)
        keys += [FocusKey(t=t, x=at(xs, t, lo, hi), y=at(ys, t, lo, hi)) for t in marks]
    return keys


def focus_expr(keys: Sequence[FocusKey], axis: str) -> str:
    """ffmpeg expression of `t` for one axis ("x" or "y"): the piecewise-linear path through the keypoints,
    written as `v0 + slope*min(max(t-t_i,0),d_i) + ...` (no nesting, holds the last value past the end)."""
    values = [getattr(k, axis) for k in keys]
    parts = [f"{values[0]:.5f}"]
    for i in range(len(keys) - 1):
        span = keys[i + 1].t - keys[i].t
        slope = (values[i + 1] - values[i]) / span if span > 1e-6 else 0.0
        if abs(slope * span) < 1e-5:
            continue
        parts.append(f"{slope:+.5f}*min(max(t-{keys[i].t:.3f},0),{span:.3f})")
    return "".join(parts)


def focus_mean(keys: Sequence[FocusKey]) -> tuple[float, float]:
    return sum(k.x for k in keys) / len(keys), sum(k.y for k in keys) / len(keys)


# ---------- I/O: frames from ffmpeg, faces from YuNet ----------


async def _read_frames(
    source: Path, start: float, duration: float, fps: float, w: int, h: int
) -> list[bytes]:
    args = [
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(source), "-vf", f"fps={fps:.4f},scale={w}:{h}",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]  # fmt: skip
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed: {err.decode(errors='replace')[-300:]}")
    size = w * h * 3
    return [out[i : i + size] for i in range(0, len(out) - size + 1, size)]


def _mouth_patch(gray: "np.ndarray", row: Sequence[float]) -> "np.ndarray | None":
    """The mouth region from YuNet's two mouth-corner landmarks, resized and contrast-normalised."""
    import cv2
    import numpy as np

    (rx, ry), (lx, ly) = (row[10], row[11]), (row[12], row[13])
    width = math.hypot(lx - rx, ly - ry)
    if width < 4:
        return None
    cx, cy = (rx + lx) / 2, (ry + ly) / 2
    x0, x1 = int(cx - 0.8 * width), int(cx + 0.8 * width)
    y0, y1 = int(cy - 0.45 * width), int(cy + 0.55 * width)
    if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0]:
        return None
    patch = cv2.resize(gray[y0:y1, x0:x1], MOUTH_PATCH, interpolation=cv2.INTER_AREA).astype(np.float32)
    return (patch - patch.mean()) / (patch.std() + 8.0)


def _detect_all(frames: Sequence[bytes], w: int, h: int, model: Path) -> list[list[Face]]:
    import cv2  # lazy: only the render worker needs it, and tracking must degrade gracefully without it
    import numpy as np

    detector = cv2.FaceDetectorYN.create(str(model), "", (w, h), SCORE_THRESHOLD, 0.3, 10)
    detector.setInputSize((w, h))
    result: list[list[Face]] = []
    # [cx, cy, mouth patch, sample index] of every recently seen face: a face the detector missed for a
    # sample or two (motion blur, an open mouth, a hand) is still compared with its own last mouth shape
    memory: list[list] = []
    for index, raw in enumerate(frames):
        image = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, found = detector.detect(image)
        faces = []
        for row in found if found is not None else []:
            x, y, fw, fh = (float(v) for v in row[:4])
            cx, cy = (x + fw / 2) / w, (y + fh / 2) / h
            patch = _mouth_patch(gray, [float(v) for v in row])
            mouth = 0.0
            if patch is not None:
                near = min(memory, key=lambda m: math.hypot(m[0] - cx, m[1] - cy), default=None)
                if near is not None and math.hypot(near[0] - cx, near[1] - cy) <= TRACK_MATCH_DIST:
                    mouth = float(np.abs(patch - near[2]).mean())
                    near[:] = [cx, cy, patch, index]
                else:
                    memory.append([cx, cy, patch, index])
            faces.append(Face(cx, cy, fw / w, fh / h, float(row[-1]), mouth))
        memory = [m for m in memory if index - m[3] <= MOUTH_MEMORY_SAMPLES]
        result.append(faces)
    return result


async def track_clip_focus(
    source: Path,
    src_in: float,
    src_out: float,
    *,
    display_w: int,
    display_h: int,
    target_w: int,
    target_h: int,
    zoom: float,
    model: Path,
) -> list[FocusKey]:
    """Camera keypoints for the clip window, or [] when tracking is unnecessary/impossible (never raises)."""
    duration = src_out - src_in
    source_aspect, target_aspect = display_w / display_h, target_w / target_h
    crop_w = min(1.0, target_aspect / source_aspect) / zoom
    crop_h = min(1.0, source_aspect / target_aspect) / zoom
    if duration <= 0 or (crop_w >= FULL_SPAN and crop_h >= FULL_SPAN):
        return []  # the crop already shows the whole frame: nothing to follow
    if not model.is_file():
        logger.warning("face tracking skipped: model %s is missing", model)
        return []
    try:
        fps = min(SAMPLE_FPS, MAX_SAMPLES / duration)
        w = ANALYSIS_WIDTH
        h = max(2, round(w * display_h / display_w / 2) * 2)
        frames = await _read_frames(source, src_in, duration, fps, w, h)
        if not frames:
            return []
        faces = await asyncio.to_thread(_detect_all, frames, w, h, model)
        times = [i / fps for i in range(len(frames))]
        subject, switches = pick_speaker(faces, times)
        return build_path(times, subject, duration, crop_w=crop_w, crop_h=crop_h, switches=switches)
    except Exception:
        logger.warning("face tracking failed: using the planner's static focus", exc_info=True)
        return []
