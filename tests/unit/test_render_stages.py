import json
import subprocess
from pathlib import Path

import pytest

from app.schemas.edit_plan import Captions, Export, Music, TextOverlay
from app.services.media.ffmpeg import FFmpegError
from app.services.media.probe import media_duration, probe
from app.services.render import final_stage
from app.services.render.captions_ass import build_ass, build_timeline
from app.services.render.concat_stage import concat_clips, concat_list_text, quote_concat_path
from app.services.render.final_stage import (
    _audio_graph,
    _video_graph,
    audio_is_silent,
    escape_filter_value,
    render_final,
)
from app.services.render.music import resolve_music_track
from tests.fakes.gemini import make_plan, make_transcript

# ---------- concat ----------


def test_concat_paths_are_quoted_and_single_quotes_escaped(tmp_path: Path) -> None:
    plain = tmp_path / "clip_0001.mp4"
    tricky = tmp_path / "it's a clip.mp4"
    assert quote_concat_path(plain) == f"'{plain.resolve()}'"
    assert quote_concat_path(tricky) == "'" + str(tricky.resolve()).replace("'", "'\\''") + "'"
    assert concat_list_text([plain, tricky]).count("\nfile ") == 1 and concat_list_text([plain]).startswith(
        "file '"
    )


async def test_concat_joins_clips_with_stream_copy(clip_two_scenes: Path, tmp_path: Path) -> None:
    a, b = tmp_path / "it's-a.mp4", tmp_path / "b.mp4"
    for path in (a, b):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(clip_two_scenes), "-t", "2", "-c", "copy", str(path)],
            check=True,
        )
    out = tmp_path / "joined.mp4"
    await concat_clips([a, b], out_path=out, workdir=tmp_path)
    assert await media_duration(out) == pytest.approx(4.0, abs=0.2)
    assert "'\\''" in (tmp_path / "list.txt").read_text()
    with pytest.raises(ValueError):
        await concat_clips([], out_path=out, workdir=tmp_path)


# ---------- music catalog ----------


def _assets(tmp_path: Path, catalog: object, files: tuple[str, ...] = ("a.mp3",)) -> Path:
    music = tmp_path / "assets" / "music"
    music.mkdir(parents=True)
    (music / "catalog.json").write_text(json.dumps(catalog))
    for name in files:
        (music / name).write_bytes(b"x")
    return tmp_path / "assets"


def test_resolve_music_track(tmp_path: Path) -> None:
    assets = _assets(tmp_path, [{"id": "upbeat_01", "file": "a.mp3", "title": "T"}])
    assert resolve_music_track("upbeat_01", assets) == (assets / "music" / "a.mp3").resolve()
    assert resolve_music_track("unknown", assets) is None
    assert resolve_music_track(None, assets) is None and resolve_music_track("", assets) is None


def test_music_catalog_cannot_point_outside_the_music_folder(tmp_path: Path) -> None:
    (tmp_path / "secret.mp3").write_bytes(b"x")
    assets = _assets(
        tmp_path, [{"id": "evil", "file": "../../secret.mp3"}, {"id": "gone", "file": "missing.mp3"}]
    )
    assert resolve_music_track("evil", assets) is None
    assert resolve_music_track("gone", assets) is None


def test_missing_or_broken_catalog_is_not_an_error(tmp_path: Path) -> None:
    assert resolve_music_track("x", tmp_path) is None
    assets = _assets(tmp_path, {"not": "a list"})
    assert resolve_music_track("x", assets) is None
    (assets / "music" / "catalog.json").write_text("{broken")
    assert resolve_music_track("x", assets) is None


# ---------- final-stage graphs ----------


def test_escape_filter_value_escapes_path_metacharacters() -> None:
    assert escape_filter_value("a:b,c'd[e];f\\g") == "a\\:b\\,c\\'d\\[e\\]\\;f\\\\g"
    assert escape_filter_value("fonts/x.ass") == "fonts/x.ass"


def test_video_graph(tmp_path: Path) -> None:
    assert _video_graph(None, tmp_path / "fonts", tmp_path) == "[0:v]null[v]"
    (tmp_path / "fonts").mkdir()
    assert (
        _video_graph(tmp_path / "captions.ass", tmp_path / "fonts", tmp_path)
        == "[0:v]ass=filename=captions.ass:fontsdir=fonts[v]"
    )
    assert "fontsdir" not in _video_graph(tmp_path / "captions.ass", tmp_path / "nope", tmp_path)


def test_audio_graphs() -> None:
    plan = make_plan([(0, 10)], music=Music(enabled=True, track_id="t", volume=0.1, fade_out_sec=2.0))
    plain = _audio_graph(plan, False, ducking=False, normalize_off=False)
    assert plain == "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"

    ducked = _audio_graph(plan, True, ducking=True, normalize_off=True)
    assert "[1:a]volume=0.1,afade=t=out:st=8.000:d=2[m]" in ducked
    assert (
        "[0:a]asplit=2[vo1][vo2]" in ducked
        and "sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300" in ducked
    )
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]" in ducked
    assert ducked.endswith("[mix]loudnorm=I=-14:TP=-1.5:LRA=11[a]")

    simple = _audio_graph(plan, True, ducking=False, normalize_off=True)
    assert "sidechaincompress" not in simple and "[m][0:a]amix=inputs=2" in simple

    compat = _audio_graph(plan, True, ducking=False, normalize_off=False)
    assert "normalize=0" not in compat and "volume=2[mix]" in compat  # level compensation for plain amix


def test_export_options_change_the_loudness_chain() -> None:
    plan = make_plan([(0, 10)], export=Export(loudnorm=False, denoise_audio=True))
    assert _audio_graph(plan, False, ducking=False, normalize_off=False) == "[0:a]afftdn=nf=-25[a]"
    off = make_plan([(0, 10)], export=Export(loudnorm=False))
    assert _audio_graph(off, False, ducking=False, normalize_off=False) == "[0:a]anull[a]"


def test_music_fade_never_exceeds_the_video_length() -> None:
    plan = make_plan([(0, 1.0)], music=Music(enabled=True, track_id="t", fade_out_sec=5.0))
    assert "afade=t=out:st=0.000:d=1" in _audio_graph(plan, True, ducking=False, normalize_off=True)
    no_fade = make_plan([(0, 4.0)], music=Music(enabled=True, track_id="t", fade_out_sec=0.0))
    assert "afade" not in _audio_graph(no_fade, True, ducking=False, normalize_off=True)


# ---------- render_final on real media ----------


@pytest.fixture(scope="module")
def music_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("music") / "track.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=3", str(path)],
        check=True,
    )
    return path


def _plan(**fields):  # noqa: ANN003, ANN202
    return make_plan([(0, 6)], **fields)


async def test_final_without_captions_or_music(clip_two_scenes: Path, tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    await render_final(
        joined=clip_two_scenes,
        ass_path=None,
        music_path=None,
        plan=_plan(),
        target_w=640,
        target_h=360,
        out_path=out,
        fonts_dir=tmp_path / "fonts",
    )
    info = await probe(str(out))
    assert (info.width, info.height, info.has_audio) == (640, 360, True)
    assert info.duration_sec == pytest.approx(6.0, abs=0.3)


async def test_final_burns_captions_overlays_and_watermark(clip_two_scenes: Path, tmp_path: Path) -> None:
    plan = _plan(
        captions=Captions(style="word_highlight"),
        overlays=[TextOverlay(text="Sarlavha", start=0, end=2)],
        watermark={"enabled": True, "text": "@bot"},
    )
    ass = build_ass(
        plan,
        make_transcript([("salom", 1.0, 1.5), ("dunyo", 1.6, 2.2)]),
        build_timeline(plan.clips),
        target_w=640,
        target_h=360,
    )
    ass_path = tmp_path / "captions.ass"
    ass_path.write_text(ass, encoding="utf-8")
    out = tmp_path / "final.mp4"
    await render_final(
        joined=clip_two_scenes,
        ass_path=ass_path,
        music_path=None,
        plan=plan,
        target_w=640,
        target_h=360,
        out_path=out,
        fonts_dir=tmp_path / "fonts",
    )
    assert (await probe(str(out))).duration_sec == pytest.approx(6.0, abs=0.3)
    plain = tmp_path / "plain.mp4"
    await render_final(
        joined=clip_two_scenes,
        ass_path=None,
        music_path=None,
        plan=plan,
        target_w=640,
        target_h=360,
        out_path=plain,
        fonts_dir=tmp_path / "fonts",
    )
    assert out.read_bytes() != plain.read_bytes()  # something was actually drawn


@pytest.mark.parametrize("ducking", [True, False])
async def test_final_mixes_music_into_one_audio_track(
    clip_two_scenes: Path, music_file: Path, tmp_path: Path, ducking: bool
) -> None:
    plan = _plan(music=Music(enabled=True, track_id="t", volume=0.2, ducking=ducking, fade_out_sec=1.0))
    out = tmp_path / "final.mp4"
    await render_final(
        joined=clip_two_scenes,
        ass_path=None,
        music_path=music_file,
        plan=plan,
        target_w=640,
        target_h=360,
        out_path=out,
        fonts_dir=tmp_path / "fonts",
    )
    info = await probe(str(out))
    assert info.has_audio and info.duration_sec == pytest.approx(
        6.0, abs=0.3
    )  # the 3 s track was looped and cut at -t


async def test_unsupported_music_filters_fall_back_instead_of_failing(
    clip_two_scenes: Path,
    music_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real = final_stage.run_ffmpeg
    graphs: list[str] = []

    async def picky(args, **kw):  # noqa: ANN001, ANN003, ANN202
        graph = args[args.index("-filter_complex") + 1]
        graphs.append(graph)
        if "sidechaincompress" in graph:
            raise FFmpegError("ffmpeg exited with code 1: No such filter: 'sidechaincompress'")
        return await real(args, **kw)

    monkeypatch.setattr(final_stage, "run_ffmpeg", picky)
    plan = _plan(music=Music(enabled=True, track_id="t", ducking=True))
    out = tmp_path / "final.mp4"
    with caplog.at_level("WARNING"):
        await render_final(
            joined=clip_two_scenes,
            ass_path=None,
            music_path=music_file,
            plan=plan,
            target_w=640,
            target_h=360,
            out_path=out,
            fonts_dir=tmp_path / "fonts",
        )
    assert len(graphs) == 2 and "sidechaincompress" not in graphs[1]
    assert "falling back" in caplog.text and (await probe(str(out))).has_audio


async def test_normalize_option_missing_falls_back_twice(
    clip_two_scenes: Path, music_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = final_stage.run_ffmpeg
    graphs: list[str] = []

    async def old_ffmpeg(args, **kw):  # noqa: ANN001, ANN003, ANN202
        graph = args[args.index("-filter_complex") + 1]
        graphs.append(graph)
        if "normalize=0" in graph:
            raise FFmpegError("ffmpeg exited with code 1: Option 'normalize' not found")
        return await real(args, **kw)

    monkeypatch.setattr(final_stage, "run_ffmpeg", old_ffmpeg)
    plan = _plan(music=Music(enabled=True, track_id="t", ducking=True))
    await render_final(
        joined=clip_two_scenes,
        ass_path=None,
        music_path=music_file,
        plan=plan,
        target_w=640,
        target_h=360,
        out_path=tmp_path / "f.mp4",
        fonts_dir=tmp_path / "fonts",
    )
    assert len(graphs) == 3 and "normalize=0" not in graphs[2]


async def test_unrelated_ffmpeg_errors_are_not_swallowed(
    clip_two_scenes: Path, music_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    async def broken(args, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(1)
        raise FFmpegError("ffmpeg exited with code 1: Invalid data found when processing input")

    monkeypatch.setattr(final_stage, "run_ffmpeg", broken)
    plan = _plan(music=Music(enabled=True, track_id="t"))
    with pytest.raises(FFmpegError, match="Invalid data"):
        await render_final(
            joined=clip_two_scenes,
            ass_path=None,
            music_path=music_file,
            plan=plan,
            target_w=640,
            target_h=360,
            out_path=tmp_path / "f.mp4",
            fonts_dir=tmp_path / "fonts",
        )
    assert len(calls) == 1


# ---------- silent audio must not go through loudnorm ----------


def test_silent_audio_skips_loudness_normalisation() -> None:
    plan = make_plan([(0, 10)], export=Export(denoise_audio=True))
    assert _audio_graph(plan, False, ducking=False, normalize_off=False, silent=True) == "[0:a]anull[a]"
    assert "loudnorm" in _audio_graph(plan, False, ducking=False, normalize_off=False, silent=False)


async def test_audio_is_silent_detects_silence_and_signal(clip_two_scenes: Path, tmp_path: Path) -> None:
    assert await audio_is_silent(clip_two_scenes) is False
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(clip_two_scenes),
            "-af",
            "volume=0",
            "-c:v",
            "copy",
            str(silent),
        ],
        check=True,
    )
    assert await audio_is_silent(silent) is True


async def test_final_render_of_a_fully_silent_video_succeeds(clip_two_scenes: Path, tmp_path: Path) -> None:
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(clip_two_scenes),
            "-af",
            "volume=0",
            "-c:v",
            "copy",
            str(silent),
        ],
        check=True,
    )
    out = tmp_path / "final.mp4"
    await render_final(
        joined=silent, ass_path=None, music_path=None, plan=_plan(), target_w=640, target_h=360,
        out_path=out, fonts_dir=tmp_path / "fonts",
    )  # fmt: skip
    assert (await probe(str(out))).has_audio
