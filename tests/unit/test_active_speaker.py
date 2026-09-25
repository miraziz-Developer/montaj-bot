import subprocess
from pathlib import Path

import pytest

from app.schemas.edit_plan import Clip, Reframe
from app.services.render.clip_stage import render_clip
from app.services.render.face_track import MODEL_NAME, Face, build_path, pick_speaker, track_clip_focus

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "assets" / "models" / MODEL_NAME
FACE_PHOTO = ROOT / "tests" / "data" / "face.jpg"
T = [i / 4 for i in range(24)]  # 6 s at 4 samples/s


def _two(a_talks: float, b_talks: float, a_x: float = 0.25, b_x: float = 0.75) -> list[Face]:
    return [Face(a_x, 0.4, 0.1, 0.1, 0.9, a_talks), Face(b_x, 0.4, 0.1, 0.1, 0.9, b_talks)]


def test_the_camera_switches_to_whoever_talks_and_cuts_when_they_start() -> None:
    frames = [_two(0.3, 0.02) if t < 3 else _two(0.02, 0.3) for t in T]
    subject, switches = pick_speaker(frames, T)
    assert [f.cx for f in subject[:12]] == [0.25] * 12 and [f.cx for f in subject[12:]] == [0.75] * 12
    assert switches == {12}  # backdated to 3.0 s, not when the smoothed activity finally crossed over
    keys = build_path(T, subject, 6.0, crop_w=0.32, crop_h=1.0, switches=switches)
    xs = [(k.t, k.x) for k in keys]
    assert all(x < 0.3 for t, x in xs if t < 2.99) and all(x > 0.7 for t, x in xs if t >= 3.0)
    assert not any(0.3 <= x <= 0.7 for _, x in xs)  # a cut, not a pan across the room


def test_a_nodding_listener_or_noise_does_not_steal_the_camera() -> None:
    frames = [_two(0.25, 0.3 if i % 4 == 0 else 0.02) for i in range(len(T))]  # B: sporadic blips
    subject, switches = pick_speaker(frames, T)
    assert switches == set() and {f.cx for f in subject} == {0.25}


def test_no_mouth_data_keeps_the_old_behaviour() -> None:
    frames = [_two(0.0, 0.0) for _ in T]
    subject, switches = pick_speaker(frames, T)
    assert switches == set() and {f.cx for f in subject} == {0.25}


def test_a_nearby_speaker_is_panned_to_not_cut() -> None:
    frames = [_two(0.3, 0.02, 0.45, 0.55) if t < 3 else _two(0.02, 0.3, 0.45, 0.55) for t in T]
    subject, switches = pick_speaker(frames, T)
    keys = build_path(T, subject, 6.0, crop_w=0.32, crop_h=1.0, switches=switches)
    assert switches and all(b.t - a.t > 0.1 for a, b in zip(keys, keys[1:], strict=False))  # no cut edge


# ---------- real faces ----------

needs_assets = pytest.mark.skipif(not (MODEL.is_file() and FACE_PHOTO.is_file()), reason="assets missing")


@pytest.fixture(scope="module")
def two_people(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """16:9, 6 s: the same person left and (mirrored) right; the left mouth moves for 3 s, then the right."""
    out = tmp_path_factory.mktemp("speakers") / "two.mp4"
    talk = "mod(floor(t*6),2)"  # the mouth "opens" (a dark lip-sized shadow) on and off, 3 times a second
    graph = (
        "[1:v]scale=260:260,split[a][b];[b]hflip[bf];"
        "[0:v][a]overlay=60:230[l];[l][bf]overlay=960:230,"
        f"drawbox=x=160:y=299:w=22:h=8:c=0x401818@0.75:t=fill:enable='lt(t,3)*{talk}',"
        f"drawbox=x=1098:y=299:w=22:h=8:c=0x401818@0.75:t=fill:enable='gte(t,3)*{talk}',format=yuv420p"
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x60707f:s=1280x720:r=30:d=6",
         "-loop", "1", "-framerate", "30", "-i", str(FACE_PHOTO), "-filter_complex", graph,
         "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", str(out)],
        check=True,
    )  # fmt: skip
    return out


@needs_assets
async def test_real_two_person_shot_frames_the_talking_face(two_people: Path, tmp_path: Path) -> None:
    keys = await track_clip_focus(
        two_people, 0.0, 6.0, display_w=1280, display_h=720, target_w=360, target_h=640, zoom=1.0, model=MODEL
    )
    assert keys, "no faces found"
    early = [k.x for k in keys if k.t <= 2.5]
    late = [k.x for k in keys if k.t >= 4.0]
    assert max(early) < 0.3, early  # the left person while they talk
    assert min(late) > 0.7, late  # then the right person
    switch = next(k.t for k in keys if k.x > 0.5)
    assert 2.5 <= switch <= 3.6  # close to when the right person started talking

    clip = Clip(id="c", src_in=0.0, src_out=6.0, reframe=Reframe(mode="fill", focus_x=0.5, focus_y=0.5))
    out = tmp_path / "tracked.mp4"
    await render_clip(two_people, clip, out_path=out, target_w=360, target_h=640, fps=30, has_audio=False,
                      audio_duration_sec=0.0, focus_track=keys)  # fmt: skip
    import cv2

    for t in (1.5, 4.5):  # a face is in the 9:16 frame both times
        frame = tmp_path / f"f{t}.png"
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(out), "-frames:v", "1", str(frame)]
        subprocess.run(cmd, check=True)
        image = cv2.imread(str(frame))
        det = cv2.FaceDetectorYN.create(str(MODEL), "", (image.shape[1], image.shape[0]), 0.6, 0.3, 10)
        assert det.detect(image)[1] is not None, f"no face in the tracked frame at {t}s"
