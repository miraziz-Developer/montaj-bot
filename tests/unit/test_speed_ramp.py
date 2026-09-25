import math
import subprocess
from pathlib import Path

import pytest

from app.schemas.edit_plan import RAMP_FAST, RAMP_SLOW, Clip, ClipAudio
from app.services.media.probe import probe
from app.services.render.captions_ass import _clip_words, build_timeline
from app.services.render.clip_stage import audio_filter, render_clip, retime
from tests.fakes.gemini import make_transcript


def test_the_ramp_keeps_the_planned_duration() -> None:
    assert RAMP_SLOW == pytest.approx(0.572, abs=0.01)
    assert math.log(RAMP_FAST / RAMP_SLOW) / (RAMP_FAST - RAMP_SLOW) == pytest.approx(1.0, abs=1e-9)


def _out_time(expr: str, t: float) -> float:
    body = expr.removeprefix("setpts='").removesuffix("/TB'")
    return eval(body, {"log": math.log, "max": max, "T": t, "STARTT": 0.0})  # noqa: S307 - our own text


@pytest.mark.parametrize("ramp", ["fast_to_slow", "slow_to_fast"])
def test_retime_maps_the_whole_clip_onto_its_planned_length(ramp: str) -> None:
    clip = Clip(id="c", src_in=2.0, src_out=6.0, speed=1.25, speed_ramp=ramp, audio=ClipAudio(mute=True))
    expr = retime(clip)
    assert _out_time(expr, 4.0) == pytest.approx(clip.out_duration, rel=1e-4)  # 4 s of source -> 3.2 s
    early = _out_time(expr, 1.0) / clip.out_duration
    assert early < 0.25 if ramp == "fast_to_slow" else early > 0.25  # the fast part covers source quickly


def test_constant_speed_is_unchanged() -> None:
    assert retime(Clip(id="c", src_in=0, src_out=2, speed=1.5)) == "setpts=PTS/1.5"


def test_a_b_roll_dub_plays_the_narration_at_1x() -> None:
    dub = ClipAudio(source="primary", primary_src_in=3.0, primary_src_out=5.0)
    clip = Clip(id="b", source_id="broll_1", src_in=0, src_out=3, speed=1.5, audio=dub)
    assert "atempo" not in audio_filter(clip)
    assert "atempo=1.5" in audio_filter(Clip(id="c", src_in=0, src_out=3, speed=1.5))


# ---------- captions follow what is heard ----------

WORDS = make_transcript([("bir", 1.0, 1.4), ("ikki", 3.2, 3.6), ("uch", 4.2, 4.6)])


def test_b_roll_captions_show_the_dubbed_narration_not_the_b_roll_time_range() -> None:
    dub = ClipAudio(source="primary", primary_src_in=3.0, primary_src_out=5.0)
    broll = Clip(id="b", source_id="broll_1", src_in=0.5, src_out=2.5, audio=dub)
    [entry] = build_timeline([broll])
    words = _clip_words(entry, WORDS, uppercase=False)
    assert [w.text for w in words] == ["ikki", "uch"]  # not "bir" (primary 1.0 s, inside the b-roll's range)
    assert words[0].start == pytest.approx(0.2)


def test_muted_and_own_sound_b_roll_clips_have_no_captions() -> None:
    muted = Clip(id="m", src_in=0, src_out=5, audio=ClipAudio(mute=True))
    own = Clip(id="o", source_id="broll_1", src_in=0, src_out=5)
    for clip in (muted, own):
        [entry] = build_timeline([clip])
        assert _clip_words(entry, WORDS, uppercase=False) == []


# ---------- real ffmpeg ----------


@pytest.mark.parametrize("ramp", ["fast_to_slow", "slow_to_fast"])
async def test_a_ramped_clip_renders_at_its_planned_duration(
    ramp: str, clip_two_scenes: Path, tmp_path: Path
) -> None:
    clip = Clip(id="c", src_in=0.5, src_out=4.5, speed_ramp=ramp, audio=ClipAudio(mute=True))
    out = tmp_path / "ramp.mp4"
    await render_clip(clip_two_scenes, clip, out_path=out, target_w=360, target_h=640, fps=30)
    info = await probe(str(out))
    assert info.duration_sec == pytest.approx(clip.out_duration, abs=0.1)
    assert info.fps == 30.0 and info.has_audio


@pytest.fixture(scope="module")
def frame_counter(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """4 s whose brightness grows with the frame number: the luma of an output frame tells the source time."""
    out = tmp_path_factory.mktemp("ramp") / "counter.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "nullsrc=s=320x180:r=30:d=4",
         "-vf", "geq=lum='16+N*1.7':cb=128:cr=128,format=yuv420p", "-c:v", "libx264", "-qp", "0",
         "-preset", "ultrafast", str(out)],
        check=True,
    )  # fmt: skip
    return out


def _luma_at(video: Path, t: float) -> float:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=8:8",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return sum(raw) / len(raw)


async def test_fast_to_slow_really_races_through_the_start(frame_counter: Path, tmp_path: Path) -> None:
    kw = {"target_w": 180, "target_h": 320, "fps": 30, "has_audio": False, "audio_duration_sec": 0.0}
    base = {"id": "c", "src_in": 0.0, "src_out": 4.0, "audio": ClipAudio(mute=True)}
    ramped, steady = tmp_path / "ramped.mp4", tmp_path / "steady.mp4"
    await render_clip(frame_counter, Clip(**base, speed_ramp="fast_to_slow"), out_path=ramped, **kw)
    await render_clip(frame_counter, Clip(**base), out_path=steady, **kw)
    # a quarter into the output the ramp has covered far more source (brighter) than constant speed...
    assert _luma_at(ramped, 1.0) > _luma_at(steady, 1.0) + 15
    # ...and both end on the same source frame
    assert _luma_at(ramped, 3.9) == pytest.approx(_luma_at(steady, 3.9), abs=12)
