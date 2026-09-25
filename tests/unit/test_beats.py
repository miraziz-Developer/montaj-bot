import subprocess
from pathlib import Path

import pytest

from app.schemas.edit_plan import Captions, Music
from app.services.render.beats import BeatGrid, best_offset, detect_beats, on_beat_fraction
from app.services.render.final_stage import _audio_graph, cut_times, render_final
from tests.fakes.gemini import make_plan


def _click_track(path: Path, bpm: float, phase: float, seconds: float = 40) -> Path:
    period = 60 / bpm
    click = f"0.8*sin(2*PI*1000*t)*exp(-60*mod(t-{phase}+20*{period},{period}))"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc='{click}':d={seconds}:s=44100",
         "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:a=0.05", "-filter_complex", "amix=inputs=2",
         str(path)],
        check=True,
    )  # fmt: skip
    return path


@pytest.mark.parametrize(("bpm", "phase"), [(128, 0.21), (100, 0.05), (90, 0.5), (140, 0.33)])
async def test_tempo_and_phase_of_a_click_track(bpm: float, phase: float, tmp_path: Path) -> None:
    grid = await detect_beats(_click_track(tmp_path / "c.wav", bpm, phase))
    assert grid is not None
    assert grid.bpm == pytest.approx(bpm, abs=0.15)
    miss = (grid.phase - phase + grid.period / 2) % grid.period - grid.period / 2
    assert abs(miss) <= 0.012


async def test_noise_and_broken_files_give_no_grid(tmp_path: Path) -> None:
    noise = tmp_path / "noise.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=20:c=white:a=0.3", str(noise)],
        check=True,
    )
    assert await detect_beats(noise) is None
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"not audio")
    assert await detect_beats(broken) is None  # never raises


def test_best_offset_puts_the_cuts_on_the_beat() -> None:
    grid = BeatGrid(period=0.5, phase=0.1)
    cuts = [1.23, 2.73, 4.23, 5.73]  # all 0.13 s past a beat of the unshifted track...
    assert on_beat_fraction(grid, cuts, 0.0) == 0.0
    offset = best_offset(grid, cuts)
    assert on_beat_fraction(grid, cuts, offset) == 1.0  # ...so starting the track 0.37 s in fixes all
    assert offset == pytest.approx(0.37, abs=0.03)
    assert best_offset(grid, []) == 0.1  # nothing to align: enter on the first beat


def test_weighted_scene_changes_win_over_jump_cuts() -> None:
    grid = BeatGrid(period=1.0, phase=0.0)
    offset = best_offset(grid, [1.0, 2.5], weights=[1.0, 2.0])
    assert on_beat_fraction(grid, [2.5], offset) == 1.0


def test_cut_times_and_the_music_offset_in_the_graph() -> None:
    plan = make_plan([(0, 2), (2, 5), (7, 9)])
    plan.clips[2].role = "cta"
    assert cut_times(plan) == ([2.0, 5.0], [1.0, 2.0])
    music = plan.model_copy(update={"music": Music(enabled=True, track_id="x")})
    graph = _audio_graph(music, True, ducking=False, normalize_off=True, music_offset=0.37)
    assert graph.startswith("[1:a]atrim=start=0.370,asetpts=PTS-STARTPTS,afade=t=in:d=0.25,volume=")
    assert "atrim" not in _audio_graph(music, True, ducking=False, normalize_off=True)


async def test_rendered_music_lands_on_the_cuts(tmp_path: Path) -> None:
    joined = tmp_path / "joined.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x808080:s=320x180:r=30:d=8",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "8", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(joined)],
        check=True,
    )  # fmt: skip
    track = _click_track(tmp_path / "track.wav", 128, 0.21)
    music = Music(enabled=True, track_id="t", volume=0.5, ducking=False, fade_out_sec=0)
    plan = make_plan([(0, 2.3), (2.3, 5.1), (5.1, 8)], captions=Captions(enabled=False), music=music)
    out = tmp_path / "final.mp4"
    await render_final(joined=joined, ass_path=None, music_path=track, plan=plan, target_w=320, target_h=180,
                       out_path=out, fonts_dir=tmp_path / "f")  # fmt: skip
    grid = await detect_beats(out)
    assert grid is not None and grid.bpm == pytest.approx(128, abs=0.5)
    cuts, _ = cut_times(plan)
    assert on_beat_fraction(grid, cuts, 0.0) == 1.0  # in the OUTPUT, both cuts sit on a beat
