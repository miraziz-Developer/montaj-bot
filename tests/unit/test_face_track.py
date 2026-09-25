import subprocess
from pathlib import Path

import pytest

from app.schemas.edit_plan import Clip, Reframe
from app.services.render.clip_stage import fill_chain, render_clip
from app.services.render.face_track import (
    MAX_SPEED,
    MODEL_NAME,
    Face,
    FocusKey,
    build_path,
    focus_expr,
    focus_mean,
    pick_subject,
    track_clip_focus,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "assets" / "models" / MODEL_NAME
FACE_PHOTO = ROOT / "tests" / "data" / "face.jpg"


def _face(cx: float, cy: float = 0.4, size: float = 0.1) -> Face:
    return Face(cx, cy, size, size, 0.9)


def _times(n: int, fps: float = 4.0) -> list[float]:
    return [i / fps for i in range(n)]


# ---------- pick_subject ----------


def test_first_pick_is_the_largest_face_then_the_one_nearest_the_previous_subject() -> None:
    speaker, bystander = _face(0.30, size=0.12), _face(0.80, size=0.05)
    frames = [[speaker, bystander], [_face(0.31, size=0.12), _face(0.60, size=0.14)]]
    picks = pick_subject(frames, _times(2))
    assert picks[0] == speaker
    assert (
        picks[1] is not None and picks[1].cx == 0.31
    )  # the bigger face on the right does not steal the camera


def test_a_subject_unseen_for_too_long_is_forgotten() -> None:
    frames = [[_face(0.2, size=0.1)], [], [], [], [], [], [], [_face(0.7, size=0.1), _face(0.3, size=0.3)]]
    picks = pick_subject(frames, _times(8))
    assert picks[-1] is not None and picks[-1].cx == 0.3  # fresh choice: the largest


# ---------- build_path ----------


def test_too_few_detections_give_no_path() -> None:
    subject: list[Face | None] = [_face(0.5)] + [None] * 9  # 10% of the samples
    assert build_path(_times(10), subject, 2.5, crop_w=0.32, crop_h=1.0) == []


def test_a_centred_subject_keeps_the_camera_still() -> None:
    keys = build_path(_times(12), [_face(0.5)] * 12, 3.0, crop_w=0.32, crop_h=1.0)
    assert {round(k.x, 4) for k in keys} == {0.5}


def test_a_moving_subject_is_followed_smoothly_within_the_speed_cap() -> None:
    times = _times(20)
    subject = [_face(0.2 + 0.03 * i) for i in range(20)]  # walks from 0.2 to 0.77 in 4.75 s
    keys = build_path(times, subject, 4.75, crop_w=0.32, crop_h=1.0)
    xs = [k.x for k in keys]
    assert xs == sorted(xs) and xs[-1] - xs[0] > 0.3  # follows to the right, never backwards
    assert all(0.16 - 1e-6 <= x <= 0.84 + 1e-6 for x in xs)  # the crop never leaves the frame
    for a, b in zip(keys, keys[1:], strict=False):
        assert abs(b.x - a.x) / (b.t - a.t) <= MAX_SPEED * 0.32 * 1.05  # no whip pans


def test_small_wobble_inside_the_dead_zone_does_not_move_the_camera() -> None:
    subject = [_face(0.5 + (0.01 if i % 2 else -0.01)) for i in range(12)]
    keys = build_path(_times(12), subject, 3.0, crop_w=0.32, crop_h=1.0)
    assert max(k.x for k in keys) - min(k.x for k in keys) < 1e-6


def test_short_gaps_are_interpolated_and_long_gaps_hold_the_last_position() -> None:
    times = _times(16)
    short_gap = [_face(0.3)] * 4 + [None] * 4 + [_face(0.7)] * 8  # 1 s gap: interpolated
    long_gap = [_face(0.3)] * 4 + [None] * 8 + [_face(0.7)] * 4  # 2 s gap: subject left, hold
    a = build_path(times, short_gap, 4.0, crop_w=0.32, crop_h=1.0)
    b = build_path(times, long_gap, 4.0, crop_w=0.32, crop_h=1.0)
    assert a[-1].x > 0.4 and b[-1].x > 0.4  # both eventually follow to the new position
    at_two_s = next(k.x for k in b if abs(k.t - 2.0) < 1e-6)
    assert at_two_s <= 0.35  # held near the last known position, not drifting across the gap


def test_an_axis_the_crop_fully_spans_is_held_still() -> None:
    subject = [_face(0.5, cy=0.2 + 0.05 * i) for i in range(10)]
    keys = build_path(_times(10), subject, 2.5, crop_w=0.32, crop_h=1.0)
    assert len({round(k.y, 4) for k in keys}) == 1


# ---------- focus_expr ----------


def _eval(expr: str, t: float) -> float:
    return float(eval(expr, {"min": min, "max": max, "t": t}))  # noqa: S307 - our own generated expression


def test_focus_expr_reproduces_the_piecewise_linear_path() -> None:
    keys = [
        FocusKey(0.0, 0.3, 0.5),
        FocusKey(0.5, 0.3, 0.5),
        FocusKey(1.0, 0.5, 0.5),
        FocusKey(2.0, 0.4, 0.5),
    ]
    expr = focus_expr(keys, "x")
    for t, expected in [(0.0, 0.3), (0.4, 0.3), (0.75, 0.4), (1.0, 0.5), (1.5, 0.45), (2.0, 0.4), (9.0, 0.4)]:
        assert _eval(expr, t) == pytest.approx(expected, abs=1e-4), t


def test_a_static_path_is_a_plain_number() -> None:
    assert focus_expr([FocusKey(0, 0.4, 0.5), FocusKey(2, 0.4, 0.5)], "x") == "0.40000"


# ---------- fill_chain with a focus track ----------


def test_fill_chain_uses_the_path_expression_and_keeps_the_static_form_without_one() -> None:
    reframe = Reframe(mode="fill", focus_x=0.42, focus_y=0.5)
    plain = fill_chain(360, 640, reframe, duration=3.0)
    assert "iw*0.4200-ow/2" in plain and "min(max(t-" not in plain
    keys = [FocusKey(0, 0.3, 0.5), FocusKey(3, 0.7, 0.5)]
    tracked = fill_chain(360, 640, reframe, duration=3.0, focus=keys)
    assert "min(max(iw*(0.30000+0.13333*min(max(t-0.000,0),3.000))-ow/2,0),iw-ow)" in tracked
    static = fill_chain(360, 640, reframe, duration=0.2, focus=keys)  # too short to animate: the mean
    assert "iw*0.5000" in static
    assert focus_mean(keys) == (0.5, 0.5)


# ---------- real faces: YuNet + ffmpeg end to end ----------

needs_assets = pytest.mark.skipif(
    not (MODEL.is_file() and FACE_PHOTO.is_file()), reason="face model or test photo missing"
)


@pytest.fixture(scope="module")
def moving_face_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """16:9, 4 s: a real face photo (NASA astronaut, public domain) slides from the left to the right."""
    out = tmp_path_factory.mktemp("face") / "moving_face.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x60707f:s=1280x720:r=30:d=4",
         "-loop", "1", "-framerate", "30", "-i", str(FACE_PHOTO),
         "-filter_complex",
         "[1:v]scale=260:260[f];[0:v][f]overlay=x='80+215*t':y=230:shortest=1,format=yuv420p",
         "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )  # fmt: skip
    return out


def _face_center_x(video: Path, t: float, tmp: Path) -> float | None:
    import cv2

    frame = tmp / f"f_{t}.png"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(frame)],
        check=True,
    )
    image = cv2.imread(str(frame))
    detector = cv2.FaceDetectorYN.create(str(MODEL), "", (image.shape[1], image.shape[0]), 0.6, 0.3, 10)
    _, faces = detector.detect(image)
    if faces is None:
        return None
    x, _, w, _ = faces[0][:4]
    return float((x + w / 2) / image.shape[1])


@needs_assets
async def test_the_tracker_follows_a_real_face_across_the_frame(moving_face_video: Path) -> None:
    keys = await track_clip_focus(
        moving_face_video, 0.0, 4.0, display_w=1280, display_h=720, target_w=360, target_h=640, zoom=1.0,
        model=MODEL,
    )  # fmt: skip
    assert keys, "no face found in a clear frontal photo"
    xs = [k.x for k in keys]
    assert xs[-1] - xs[0] > 0.4 and xs == sorted(xs)


@needs_assets
async def test_tracked_reframe_keeps_the_face_in_a_9x16_crop_where_the_static_focus_loses_it(
    moving_face_video: Path, tmp_path: Path
) -> None:
    keys = await track_clip_focus(
        moving_face_video, 0.0, 4.0, display_w=1280, display_h=720, target_w=360, target_h=640, zoom=1.0,
        model=MODEL,
    )  # fmt: skip
    clip = Clip(id="c", src_in=0.0, src_out=4.0, reframe=Reframe(mode="fill", focus_x=0.5, focus_y=0.5))
    kw = {"target_w": 360, "target_h": 640, "fps": 30, "has_audio": False, "audio_duration_sec": 0.0}
    tracked, static = tmp_path / "tracked.mp4", tmp_path / "static.mp4"
    await render_clip(moving_face_video, clip, out_path=tracked, focus_track=keys, **kw)
    await render_clip(moving_face_video, clip, out_path=static, **kw)

    for t in (0.4, 2.0, 3.5):  # start, middle and end of the walk
        centre = _face_center_x(tracked, t, tmp_path)
        assert centre is not None and 0.1 <= centre <= 0.9, f"tracked crop lost the face at t={t}"
    assert _face_center_x(static, 3.5, tmp_path) is None  # the static centre crop shows an empty wall by then
