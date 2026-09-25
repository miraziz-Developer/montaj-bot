import re
import subprocess
from pathlib import Path

import pytest

from app.schemas.edit_plan import Clip, EditPlan, Sfx, TextOverlay
from app.services.media.probe import probe
from app.services.render.final_stage import _audio_graph, _video_graph, render_final
from app.services.render.looks import LOOKS, look_filter
from app.services.render.sfx import MIN_GAP_SEC, plan_cues
from tests.fakes.gemini import make_plan


def _plan(clips: list[Clip], **fields) -> EditPlan:  # noqa: ANN003
    return EditPlan(title="t", style_preset="dynamic_reels", clips=clips, **fields)


def _clip(i: int, a: float, b: float, **kw) -> Clip:  # noqa: ANN003
    return Clip(id=f"c{i}", src_in=a, src_out=b, **kw)


# ---------- looks ----------


def test_natural_is_the_identity_and_every_other_look_has_a_chain() -> None:
    assert look_filter("natural") == "" and look_filter("nonsense") == ""
    assert all(look_filter(name) for name in LOOKS if name != "natural")


def test_look_goes_in_front_of_the_captions_in_the_video_graph(tmp_path: Path) -> None:
    ass = tmp_path / "c.ass"
    graph = _video_graph(ass, tmp_path / "fonts", tmp_path, "warm")
    assert graph.startswith("[0:v]colorbalance=") and graph.endswith("[v]") and ",ass=" in graph
    assert _video_graph(None, tmp_path, tmp_path, "natural") == "[0:v]null[v]"
    assert _video_graph(None, tmp_path, tmp_path, "bw") == f"[0:v]{look_filter('bw')}[v]"


# ---------- sfx cue planning ----------


def test_sfx_off_means_no_cues() -> None:
    assert plan_cues(make_plan([(0, 2), (2, 4)])) == []


def test_structural_cuts_get_a_whoosh_that_peaks_on_the_cut() -> None:
    clips = [_clip(1, 0, 3, role="hook"), _clip(2, 3, 6, role="body"), _clip(3, 6, 9, role="body")]
    cues = plan_cues(_plan(clips, sfx=Sfx(enabled=True)))
    assert [c.kind for c in cues] == ["whoosh"]  # hook->body is structural; body->body jump cut is not
    assert cues[0].t == pytest.approx(3.0 - 0.32)


def test_plain_jump_cuts_are_sparse_and_cues_keep_a_minimum_gap() -> None:
    clips = [_clip(i, i * 1.0, i * 1.0 + 1.0) for i in range(1, 12)]  # eleven 1 s body clips
    cues = plan_cues(_plan(clips, sfx=Sfx(enabled=True)))
    assert len(cues) == 2  # 10 plain jump cuts in 11 s: an accent at the first and one 8 s later
    assert all(b.t - a.t >= MIN_GAP_SEC for a, b in zip(cues, cues[1:], strict=False))


def test_rhythm_crossfades_are_not_scene_changes() -> None:
    fade = {"transition_in": {"type": "crossfade", "duration": 0.12}}
    clips = [_clip(1, 0, 3), *[_clip(i, i * 3.0, i * 3.0 + 3.0, **fade) for i in range(2, 5)]]
    cues = plan_cues(_plan(clips, sfx=Sfx(enabled=True)))
    assert [round(c.t, 2) for c in cues] == [2.68]  # crossfaded cuts at 3, 6, 9: only the first is accented


def test_overlays_get_a_pop() -> None:
    plan = _plan(
        [_clip(1, 0, 6)], sfx=Sfx(enabled=True), overlays=[TextOverlay(text="Salom", start=1, end=3)]
    )
    assert [(c.kind, c.t) for c in plan_cues(plan)] == [("pop", 1.0)]


def test_a_b_roll_switch_counts_as_structural() -> None:
    clips = [_clip(1, 0, 3), _clip(2, 0, 2, source_id="broll_1")]
    assert len(plan_cues(_plan(clips, sfx=Sfx(enabled=True)))) == 1


# ---------- graph text ----------


def test_audio_graph_adds_effects_after_loudness_normalisation_then_limits() -> None:
    plan = _plan([_clip(1, 0, 3, role="hook"), _clip(2, 3, 6)], sfx=Sfx(enabled=True))
    cues = plan_cues(plan)
    graph = _audio_graph(plan, False, ducking=False, normalize_off=True, cues=cues)
    assert "anoisesrc" in graph and "[voice][fx0]amix=inputs=2" in graph
    assert graph.index("loudnorm") < graph.index("[voice][fx0]amix") < graph.index("alimiter")
    assert graph.endswith("[a]") and graph.count("[a]") == 1
    music = _audio_graph(plan, True, ducking=True, normalize_off=True, cues=cues)
    assert "sidechaincompress" in music and "[voice][fx0]amix=inputs=2" in music and music.endswith("[a]")


# ---------- real ffmpeg ----------


def _rms_db(video: Path, start: float, duration: float) -> float:
    err = subprocess.run(
        ["ffmpeg", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(video), "-vn",
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr  # fmt: skip
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", err).group(1))  # type: ignore[union-attr]


def _true_peak_db(video: Path) -> float:
    err = subprocess.run(
        ["ffmpeg", "-i", str(video), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    return float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", err)[-1])


async def test_final_render_with_look_and_effects_keeps_duration_and_adds_audible_effects(
    clip_two_scenes: Path, tmp_path: Path
) -> None:
    def plan(sfx: bool) -> EditPlan:
        p = make_plan([(0, 3), (3, 6)], look="cinematic", sfx=Sfx(enabled=sfx, volume=0.4))
        p.clips[1].role = "cta"  # body -> cta: a structural cut at 3.0 s
        return p

    kw = {"joined": clip_two_scenes, "ass_path": None, "music_path": None, "target_w": 640, "target_h": 360}
    wet, dry = tmp_path / "wet.mp4", tmp_path / "dry.mp4"
    await render_final(plan=plan(True), out_path=wet, fonts_dir=tmp_path / "f", **kw)
    await render_final(plan=plan(False), out_path=dry, fonts_dir=tmp_path / "f", **kw)
    [cue] = plan_cues(plan(True))
    info = await probe(str(wet))
    assert info.has_audio and info.duration_sec == pytest.approx(6.0, abs=0.3)
    assert _rms_db(wet, cue.t + 0.2, 0.2) > _rms_db(dry, cue.t + 0.2, 0.2) + 0.5  # the whoosh is there
    assert _rms_db(wet, 1.0, 0.5) == pytest.approx(_rms_db(dry, 1.0, 0.5), abs=0.5)  # nothing elsewhere
    assert _true_peak_db(wet) <= -1.0  # the limiter holds the peaks


@pytest.mark.parametrize("look", [name for name in LOOKS if name != "natural"])
async def test_every_look_renders(look: str, clip_two_scenes: Path, tmp_path: Path) -> None:
    out = tmp_path / f"{look}.mp4"
    await render_final(
        joined=clip_two_scenes, ass_path=None, music_path=None, plan=make_plan([(0, 6)], look=look),
        target_w=640, target_h=360, out_path=out, fonts_dir=tmp_path / "fonts",
    )  # fmt: skip
    assert (await probe(str(out))).width == 640
