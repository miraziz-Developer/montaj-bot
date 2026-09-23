import re
import subprocess
from pathlib import Path

import pytest

from app.schemas.edit_plan import Clip, ClipAudio, Reframe
from app.services.media.ffmpeg import run_ffmpeg
from app.services.media.probe import media_duration, probe
from app.services.render.clip_stage import (
    MIN_ANIMATE_SEC,
    audio_filter,
    color_polish,
    fill_chain,
    fit_blur_graph,
    hdr_to_sdr,
    render_clip,
    stabilize,
)
from app.services.render.engine import output_resolution

# ---------- output_resolution ----------


@pytest.mark.parametrize(
    ("aspect", "src", "kwargs", "expected"),
    [
        ("9:16", (1920, 1080), {}, (1080, 1920)),
        ("16:9", (1080, 1920), {}, (1920, 1080)),
        ("1:1", (1920, 1080), {}, (1080, 1080)),
        ("original", (1920, 1080), {}, (1920, 1080)),
        ("original", (4000, 3000), {}, (1440, 1080)),  # short side clamped to 1080
        ("original", (2160, 3840), {}, (1080, 1920)),
        ("original", (1001, 563), {}, (1000, 562)),  # odd source -> even output
        ("original", (641, 361), {}, (640, 360)),
        ("9:16", (1920, 1080), {"max_short_side": 720}, (720, 1280)),  # light mode
        ("16:9", (1920, 1080), {"max_short_side": 720}, (1280, 720)),
        ("1:1", (1920, 1080), {"max_short_side": 720}, (720, 720)),
        ("original", (1920, 1080), {"max_short_side": 720}, (1280, 720)),
        ("9:16", (1920, 1080), {"max_short_side": 2000}, (1080, 1920)),  # never above 1080
    ],
)
def test_output_resolution(
    aspect: str, src: tuple[int, int], kwargs: dict, expected: tuple[int, int]
) -> None:
    width, height = output_resolution(aspect, *src, **kwargs)
    assert (width, height) == expected
    assert width % 2 == 0 and height % 2 == 0


def test_output_resolution_rejects_unknown_aspect() -> None:
    with pytest.raises(ValueError):
        output_resolution("4:3", 1920, 1080)


# ---------- filter strings ----------


def test_fill_chain_is_computed_in_python_with_ffmpeg_min_max() -> None:
    # duration omitted (0.0) -> below MIN_ANIMATE_SEC -> the exact pre-Ken-Burns static chain
    assert fill_chain(1080, 1920, Reframe()) == (
        "scale=w=1080:h=1920:force_original_aspect_ratio=increase:force_divisible_by=2,"
        "crop=1080:1920:x='min(max(iw*0.5000-540,0),iw-1080)':y='min(max(ih*0.5000-960,0),ih-1920)'"
    )


def test_fill_chain_zoom_and_focus() -> None:
    chain = fill_chain(1080, 1920, Reframe(zoom=1.15, focus_x=0.25, focus_y=0.4))
    assert chain.startswith("scale=w=1242:h=2208:")  # 1080*1.15 and 1920*1.15, made even
    assert (
        "x='min(max(iw*0.2500-540,0),iw-1080)'" in chain and "y='min(max(ih*0.4000-960,0),ih-1920)'" in chain
    )


def test_fill_chain_short_clip_stays_static_even_with_duration_given() -> None:
    """Below MIN_ANIMATE_SEC: identical to the no-`duration` (static) chain."""
    assert fill_chain(1080, 1920, Reframe(), duration=0.2) == fill_chain(1080, 1920, Reframe())


def test_fill_chain_animates_a_push_in_over_duration() -> None:
    chain = fill_chain(1080, 1920, Reframe(zoom=1.0), duration=3.0)
    # pre-scale covers the END (most zoomed-in) state: zoom * 1.08
    assert chain.startswith("scale=w=1166:h=2074:")  # round(1080*1.08), round(1920*1.08)
    assert "crop='min(1166.4-(86.4)*min(t/3.0000,1),iw)':'min(2073.6-(153.6)*min(t/3.0000,1),ih)'" in chain
    assert "x='min(max(iw*0.5000-ow/2,0),iw-ow)'" in chain
    assert "y='min(max(ih*0.5000-oh/2,0),ih-oh)'" in chain
    assert chain.endswith(",scale=1080:1920")


def test_fill_chain_animation_boundary_is_exactly_min_animate_sec() -> None:
    static = fill_chain(1080, 1920, Reframe(), duration=MIN_ANIMATE_SEC - 0.001)
    animated = fill_chain(1080, 1920, Reframe(), duration=MIN_ANIMATE_SEC)
    assert "crop=1080:1920:" in static  # unanimated form
    assert ",scale=1080:1920" in animated  # animated form has the trailing normalizing scale


def test_fit_blur_graph_keeps_ffmpeg_variables_literal() -> None:
    graph = fit_blur_graph(1080, 1920, 1.5, 30)
    assert "overlay=(W-w)/2:(H-h)/2,setpts=PTS/1.5,fps=30,format=yuv420p[v]" in graph
    assert graph.startswith(f"[0:v]{color_polish()}[polished];[polished]split=2[a][b];")
    assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg]" in graph


def test_fit_blur_graph_inscribed_square_crops_before_anything_else() -> None:
    graph = fit_blur_graph(1080, 1920, 1.0, 30, inscribed_square=True)
    assert graph.startswith("[0:v]crop=w='trunc(min(iw,ih)*0.68/2)*2'")
    assert "flags=lanczos" in graph and graph.endswith("format=yuv420p[v]")
    assert fit_blur_graph(1080, 1920, 1.0, 30) == fit_blur_graph(1080, 1920, 1.0, 30, inscribed_square=False)


def test_audio_filter_omits_atempo_at_normal_speed_and_mutes_with_volume_zero() -> None:
    assert audio_filter(Clip(id="c", src_in=0, src_out=2)) == (
        "volume=1,aresample=48000,aformat=channel_layouts=stereo,apad"
    )
    fast = Clip(id="c", src_in=0, src_out=2, speed=1.5, audio=ClipAudio(volume=0.8))
    assert audio_filter(fast) == "atempo=1.5,volume=0.8,aresample=48000,aformat=channel_layouts=stereo,apad"
    assert audio_filter(Clip(id="c", src_in=0, src_out=2, audio=ClipAudio(mute=True))).startswith("volume=0,")


# ---------- render_clip on real media ----------


async def _mean_volume(path: Path) -> float:
    _, _, err = await run_ffmpeg(
        ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"], timeout=60
    )
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", err).group(1))


async def test_speed_changes_the_output_duration(clip_two_scenes: Path, tmp_path: Path) -> None:
    out = tmp_path / "c.mp4"
    clip = Clip(id="c1", src_in=0.5, src_out=3.5, speed=1.5)  # 3 s of source at 1.5x = 2 s
    await render_clip(clip_two_scenes, clip, out_path=out, target_w=360, target_h=640, fps=30)
    assert await media_duration(out) == pytest.approx(2.0, abs=0.15)
    info = await probe(str(out))
    assert (info.width, info.height, info.fps, info.has_audio) == (360, 640, 30.0, True)


async def test_normal_speed_cut_is_accurate(clip_two_scenes: Path, tmp_path: Path) -> None:
    out = tmp_path / "c.mp4"
    await render_clip(
        clip_two_scenes,
        Clip(id="c1", src_in=1.3, src_out=3.3),
        out_path=out,
        target_w=360,
        target_h=640,
        fps=30,
    )
    assert await media_duration(out) == pytest.approx(2.0, abs=0.1)


async def test_mute_keeps_an_audio_track_that_is_silent(clip_two_scenes: Path, tmp_path: Path) -> None:
    loud, muted = tmp_path / "loud.mp4", tmp_path / "muted.mp4"
    kw = {"target_w": 360, "target_h": 640, "fps": 30}
    await render_clip(clip_two_scenes, Clip(id="a", src_in=0, src_out=2), out_path=loud, **kw)
    await render_clip(
        clip_two_scenes, Clip(id="b", src_in=0, src_out=2, audio=ClipAudio(mute=True)), out_path=muted, **kw
    )
    assert (await probe(str(muted))).has_audio
    assert await _mean_volume(loud) > -40
    assert await _mean_volume(muted) < -60
    assert await media_duration(muted) == pytest.approx(2.0, abs=0.15)


async def test_primary_dub_audio_is_pulled_from_audio_source_not_the_clips_own_file(
    clip_no_audio: Path, clip_two_scenes: Path, tmp_path: Path
) -> None:
    """P13 B-roll dub: `source` (the B-roll video) has NO audio track at all here - if `audio_source`
    weren't actually wired in, this would silently fall back to `anullsrc` (silence) instead of the
    primary's loud tone."""
    out = tmp_path / "c.mp4"
    clip = Clip(
        id="c1",
        source_id="broll_1",
        src_in=0.5,
        src_out=2.5,
        role="broll",
        audio=ClipAudio(source="primary", primary_src_in=0.0, primary_src_out=2.0),
    )
    await render_clip(
        clip_no_audio, clip, out_path=out, target_w=360, target_h=640, fps=30, audio_source=clip_two_scenes
    )
    info = await probe(str(out))
    assert info.has_audio
    assert await _mean_volume(out) > -40  # clip_two_scenes' tone, not silence
    assert await media_duration(out) == pytest.approx(2.0, abs=0.15)


def _pixel(path: Path, x: int, y: int) -> tuple[int, int, int]:
    """RGB of a 4x4 patch (averaged) at x,y of the file's first frame."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
         "-vf", f"crop=4:4:{x}:{y},scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    return raw[0], raw[1], raw[2]


async def test_round_video_note_shows_only_real_picture_no_white_corner_box(
    clip_round_note: Path, tmp_path: Path
) -> None:
    """Regression (real user complaint): a round Telegram note rendered whole showed its white mask corners
    as a hard-edged box between two blur bands. With `round_note` the inscribed square is cropped out first,
    so the foreground's corners are the real (red) picture. 360x640 target: the square is y=140..500."""
    kw = {"target_w": 360, "target_h": 640, "fps": 30}
    clip = Clip(id="c", src_in=0, src_out=1.5, reframe=Reframe(mode="fit_blur"))
    old, new = tmp_path / "old.mp4", tmp_path / "new.mp4"
    await render_clip(clip_round_note, clip, out_path=old, round_note=False, **kw)
    await render_clip(clip_round_note, clip, out_path=new, round_note=True, **kw)

    corner_old, corner_new = _pixel(old, 6, 146), _pixel(new, 6, 146)
    assert min(corner_old) > 200  # the old whole-frame path: white mask corner inside the frame
    assert corner_new[0] > 200 and corner_new[1] < 90 and corner_new[2] < 90  # now: real red picture
    assert _pixel(new, 350, 492)[1] < 90  # opposite corner too


async def test_clip_past_the_audio_tracks_own_end_still_yields_a_silent_audio_stream(
    clip_audio_shorter_than_video: Path, tmp_path: Path
) -> None:
    """Regression: a source can report has_audio=True yet its audio stream is SHORTER than the video (mic
    cut out early). A clip whose window starts after the audio ends must still get a silent track, not no
    audio stream at all - `concat`/`xfade` require every clip to have identical stream layouts."""
    out = tmp_path / "c.mp4"
    await render_clip(
        clip_audio_shorter_than_video,
        Clip(id="c1", src_in=4.5, src_out=5.5),  # entirely past the 4 s audio track
        out_path=out,
        target_w=360,
        target_h=640,
        fps=30,
        has_audio=True,
        audio_duration_sec=4.0,
    )
    info = await probe(str(out))
    assert info.has_audio
    assert await _mean_volume(out) < -60  # the added track is silence, not an error


async def test_source_without_audio_still_yields_an_audio_stream(clip_no_audio: Path, tmp_path: Path) -> None:
    out = tmp_path / "c.mp4"
    await render_clip(
        clip_no_audio,
        Clip(id="c1", src_in=0.5, src_out=2.5),
        out_path=out,
        target_w=360,
        target_h=640,
        fps=30,
    )
    info = await probe(str(out))
    assert info.has_audio and info.duration_sec == pytest.approx(2.0, abs=0.15)
    assert await _mean_volume(out) < -60  # the added track is silence


@pytest.mark.parametrize("mode", ["fill", "fit_blur"])
@pytest.mark.parametrize(
    ("source", "target"),
    [("clip_two_scenes", (360, 640)), ("clip_portrait", (640, 360)), ("clip_two_scenes", (480, 480))],
)
async def test_both_reframe_modes_produce_the_requested_size(
    request: pytest.FixtureRequest, tmp_path: Path, mode: str, source: str, target: tuple[int, int]
) -> None:
    clip = Clip(id="c1", src_in=0, src_out=1.5, reframe=Reframe(mode=mode))  # type: ignore[arg-type]
    out = tmp_path / "c.mp4"
    await render_clip(
        request.getfixturevalue(source), clip, out_path=out, target_w=target[0], target_h=target[1], fps=30
    )
    info = await probe(str(out))
    assert (info.width, info.height) == target
    assert info.duration_sec == pytest.approx(1.5, abs=0.15)


def test_color_polish_has_no_brightness_or_chroma_sharpening() -> None:
    """Deliberately conservative: contrast/saturation only, no brightness (clipping risk), luma-only
    sharpen."""
    polish = color_polish()
    assert "contrast=" in polish and "saturation=" in polish
    assert "brightness=" not in polish
    assert "ca=0.0" in polish  # chroma sharpen amount is zero


def _tonemapped_luma(trc: str, signal: float) -> float:
    """SDR luma (0-255) after `hdr_to_sdr()` for a flat HDR patch at the given 0..1 signal level."""
    y10 = int(64 + signal * 876)
    tags = f"colorspace=bt2020nc:color_primaries=bt2020:color_trc={trc}:range=tv"
    x264 = f"colorprim=bt2020:transfer={trc}:colormatrix=bt2020nc"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.2:r=10"]
    src = subprocess.run(  # encode the HDR patch, then decode it through the production chain
        [*cmd, "-vf", f"format=yuv420p10le,geq=lum={y10}:cb=512:cr=512,setparams={tags}",
         "-c:v", "libx264", "-crf", "8", "-profile:v", "high10", "-pix_fmt", "yuv420p10le",
         "-x264-params", x264, "-f", "matroska", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    stats = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "-", "-frames:v", "1", "-vf",
         f"{hdr_to_sdr()},signalstats,metadata=print:file=-", "-f", "null", "-"],
        input=src, check=True, capture_output=True,
    ).stdout.decode()  # fmt: skip
    return float(re.search(r"YAVG=([\d.]+)", stats).group(1))


@pytest.mark.parametrize(
    ("trc", "signal", "low", "high"),
    [
        ("arib-std-b67", 0.50, 130, 160),  # HLG mid signal: BT.2408 target ~145
        ("arib-std-b67", 0.75, 195, 235),  # HLG diffuse white must land near SDR white (old chain: 182)
        ("smpte2084", 0.5807, 190, 235),  # PQ 203 nit diffuse white (old chain: 181)
        ("smpte2084", 0.9026, 200, 240),  # 4000 nit highlight: rolled off, legal range (old chain: 254)
    ],
)
def test_hdr_tone_map_keeps_diffuse_white_bright_and_highlights_legal(
    trc: str, signal: float, low: int, high: int
) -> None:
    assert low <= _tonemapped_luma(trc, signal) <= high


async def test_deshake_is_opt_in_not_applied_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """deshake was ~57% of clip render time and can fight deliberate pans: it only runs when asked."""
    from app.services.render import clip_stage

    seen: list[list[str]] = []

    async def fake_run(args: list[str], **_kw: object) -> None:
        seen.append(args)

    monkeypatch.setattr(clip_stage, "run_ffmpeg", fake_run)
    kw = {"target_w": 360, "target_h": 640, "fps": 30, "has_audio": True, "audio_duration_sec": 9.0}
    clip = Clip(id="c", src_in=0, src_out=2)
    await render_clip(Path("in.mp4"), clip, out_path=tmp_path / "a.mp4", **kw)
    await render_clip(Path("in.mp4"), clip, out_path=tmp_path / "b.mp4", shake_fix=True, **kw)
    default_vf, opted_in_vf = (a[a.index("-vf") + 1] for a in seen)
    assert "deshake" not in default_vf
    assert opted_in_vf.startswith("deshake,")


def test_stabilize_is_deshake() -> None:
    assert stabilize() == "deshake"


async def test_fit_blur_mode_applies_color_polish_and_still_renders(
    clip_two_scenes: Path, tmp_path: Path
) -> None:
    out = tmp_path / "c.mp4"
    clip = Clip(id="c1", src_in=0, src_out=1.5, reframe=Reframe(mode="fit_blur"))  # type: ignore[arg-type]
    await render_clip(clip_two_scenes, clip, out_path=out, target_w=360, target_h=640, fps=30)
    info = await probe(str(out))
    assert (info.width, info.height) == (360, 640)
    assert info.duration_sec == pytest.approx(1.5, abs=0.15)


async def test_zoom_and_focus_at_the_edges_still_fit(clip_two_scenes: Path, tmp_path: Path) -> None:
    for i, (fx, fy) in enumerate([(0.0, 0.0), (1.0, 1.0), (0.5, 0.5)]):
        clip = Clip(id="c1", src_in=0, src_out=1, reframe=Reframe(zoom=1.6, focus_x=fx, focus_y=fy))
        out = tmp_path / f"c{i}.mp4"
        await render_clip(clip_two_scenes, clip, out_path=out, target_w=360, target_h=640, fps=30)
        assert (await probe(str(out))).width == 360


async def test_all_clips_share_stream_parameters_so_stream_copy_join_is_valid(
    clip_two_scenes: Path, clip_no_audio: Path, tmp_path: Path
) -> None:
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    kw = {"target_w": 360, "target_h": 640, "fps": 30}
    await render_clip(clip_two_scenes, Clip(id="a", src_in=0, src_out=1), out_path=a, **kw)
    await render_clip(clip_no_audio, Clip(id="b", src_in=0, src_out=1), out_path=b, **kw)
    sa, sb = await probe(str(a)), await probe(str(b))
    assert (sa.width, sa.height, sa.fps, sa.video_codec, sa.has_audio) == (
        sb.width,
        sb.height,
        sb.fps,
        sb.video_codec,
        sb.has_audio,
    )
