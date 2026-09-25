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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True, slots=True)
class Face:
    cx: float  # centre, 0..1 of frame width
    cy: float
    w: float  # size, 0..1 of frame width/height
    h: float
    score: float


@dataclass(frozen=True, slots=True)
class FocusKey:
    t: float  # seconds from the clip's start, in SOURCE time (the crop filter runs before speed changes)
    x: float  # 0..1 of the frame width
    y: float  # 0..1 of the frame height


# ---------- pure logic (unit-tested without OpenCV or ffmpeg) ----------


def pick_subject(frames: Sequence[Sequence[Face]], times: Sequence[float]) -> list[Face | None]:
    """One face per sample: the largest at first, then the one nearest the previous subject, so a bystander
    crossing the shot does not steal the camera. A subject unseen for STALE_SUBJECT_SEC is forgotten."""
    chosen: list[Face | None] = []
    prev: Face | None = None
    prev_t = 0.0
    for faces, t in zip(frames, times, strict=True):
        if not faces:
            chosen.append(None)
            continue
        if prev is not None and t - prev_t > STALE_SUBJECT_SEC:
            prev = None
        if prev is None:
            pick = max(faces, key=lambda f: f.w * f.h)
        else:
            anchor = prev
            pick = max(faces, key=lambda f: f.w * f.h - 3.0 * math.hypot(f.cx - anchor.cx, f.cy - anchor.cy))
        chosen.append(pick)
        prev, prev_t = pick, t
    return chosen


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


def _camera(times: Sequence[float], target: Sequence[float], crop: float) -> list[float]:
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
        if abs(error) > dead:
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
) -> list[FocusKey]:
    """Raw per-sample detections -> keypoints of a smooth camera path (empty = no reliable subject)."""
    if not times or duration <= 0:
        return []
    detected = sum(1 for f in subject if f is not None)
    if detected / len(subject) < MIN_DETECTED_FRACTION:
        return []
    xs = _camera(times, _fill_gaps(times, [f.cx if f else None for f in subject]), crop_w)
    ys = _camera(times, _fill_gaps(times, [f.cy if f else None for f in subject]), crop_h)

    def at(series: Sequence[float], t: float) -> float:
        if t <= times[0]:
            return series[0]
        for i in range(1, len(times)):
            if t <= times[i]:
                share = (t - times[i - 1]) / max(times[i] - times[i - 1], 1e-6)
                return series[i - 1] * (1 - share) + series[i] * share
        return series[-1]

    marks = [0.0]
    while marks[-1] + KEY_INTERVAL_SEC < duration - 1e-6:
        marks.append(marks[-1] + KEY_INTERVAL_SEC)
    marks.append(duration)
    return [FocusKey(t=t, x=at(xs, t), y=at(ys, t)) for t in marks]


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


def _detect_all(frames: Sequence[bytes], w: int, h: int, model: Path) -> list[list[Face]]:
    import cv2  # lazy: only the render worker needs it, and tracking must degrade gracefully without it
    import numpy as np

    detector = cv2.FaceDetectorYN.create(str(model), "", (w, h), SCORE_THRESHOLD, 0.3, 10)
    detector.setInputSize((w, h))
    result: list[list[Face]] = []
    for raw in frames:
        image = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
        _, found = detector.detect(image)
        faces = []
        if found is not None:
            for row in found:
                x, y, fw, fh = (float(v) for v in row[:4])
                faces.append(Face((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, float(row[-1])))
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
        return build_path(times, pick_subject(faces, times), duration, crop_w=crop_w, crop_h=crop_h)
    except Exception:
        logger.warning("face tracking failed: using the planner's static focus", exc_info=True)
        return []
