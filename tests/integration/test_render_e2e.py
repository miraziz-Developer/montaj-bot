"""Full render pipeline on synthetic media (real ffmpeg; no network, no DB)."""

import json
import subprocess
from pathlib import Path

import pytest

from app.core.errors import RenderError
from app.schemas.edit_plan import Captions, ClipAudio, Music, TextOverlay
from app.services.media.probe import probe
from app.services.render import engine
from app.services.render.engine import render_plan
from tests.fakes.gemini import make_plan, make_transcript

pytestmark = pytest.mark.slow

TRANSCRIPT = make_transcript(
    [("salom", 0.5, 1.0), ("dunyo", 1.1, 1.7), ("bu", 3.4, 3.7), ("sinov", 3.8, 4.4), ("videosi", 4.5, 5.2)]
)


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    root = tmp_path / "assets"
    (root / "fonts").mkdir(parents=True)
    (root / "music").mkdir()
    (root / "music" / "catalog.json").write_text("[]")
    return root


@pytest.fixture(scope="module")
def sine_mp3(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mp3") / "t1.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=2", str(path)],
        check=True,
    )
    return path


def _plan(**fields):  # noqa: ANN003, ANN202
    return make_plan([(0.0, 2.5), (3.2, 5.5)], **fields)  # 4.6 s of output


async def test_full_render_1080p_9x16_with_captions(
    clip_two_scenes: Path, tmp_path: Path, assets: Path
) -> None:
    out, workdir = tmp_path / "final.mp4", tmp_path / "work"
    stats = await render_plan(
        source=clip_two_scenes, plan=_plan(captions=Captions(style="word_highlight")),
        transcript=TRANSCRIPT, workdir=workdir, out_path=out, assets_dir=assets,
    )  # fmt: skip
    info = await probe(str(out))
    assert (info.width, info.height, info.fps) == (1080, 1920, 30.0) and info.has_audio
    assert info.video_codec == "h264" and info.duration_sec == pytest.approx(4.6, abs=0.4)
    assert stats.duration_sec == info.duration_sec and stats.size_bytes == out.stat().st_size
    assert (workdir / "captions.ass").exists()
    assert not (workdir / "clips").exists() and not (workdir / "joined.mp4").exists()  # temp files cleaned
    print(f"\n[render] 4.6 s of 1080x1920 output rendered in {stats.render_seconds:.1f}s")


@pytest.mark.parametrize(
    ("aspect", "size"), [("16:9", (640, 360)), ("1:1", (360, 360)), ("original", (640, 360))]
)
async def test_other_aspects_in_light_mode(
    clip_two_scenes: Path, tmp_path: Path, assets: Path, aspect: str, size: tuple[int, int]
) -> None:
    out = tmp_path / "final.mp4"
    await render_plan(
        source=clip_two_scenes, plan=_plan(aspect=aspect, captions=Captions(enabled=False)),
        transcript=TRANSCRIPT, workdir=tmp_path / "w", out_path=out, assets_dir=assets, max_short_side=360,
    )  # fmt: skip
    info = await probe(str(out))
    assert (info.width, info.height) == size


async def test_music_overlays_and_watermark(
    clip_two_scenes: Path, tmp_path: Path, assets: Path, sine_mp3: Path
) -> None:
    (assets / "music" / "t1.mp3").write_bytes(sine_mp3.read_bytes())
    (assets / "music" / "catalog.json").write_text(
        json.dumps([{"id": "t1", "file": "t1.mp3", "title": "sine", "mood": "calm"}])
    )
    plan = _plan(
        captions=Captions(enabled=False),
        music=Music(enabled=True, track_id="t1", volume=0.2, ducking=True, fade_out_sec=1.0),
        overlays=[
            TextOverlay(text="Yangi mahsulot", start=0.2, end=2.0),
            TextOverlay(text="Qo‘ng‘iroq qiling", start=3.5, end=4.6, style="cta"),
        ],
        watermark={"enabled": True, "text": "@video_editor_uzbot"},
    )
    out, workdir = tmp_path / "final.mp4", tmp_path / "w"
    await render_plan(
        source=clip_two_scenes,
        plan=plan,
        transcript=TRANSCRIPT,
        workdir=workdir,
        out_path=out,
        assets_dir=assets,
        max_short_side=360,
    )
    info = await probe(str(out))
    assert info.has_audio and info.duration_sec == pytest.approx(4.6, abs=0.4)
    assert (workdir / "captions.ass").exists()  # overlays + watermark are burned in even with captions off


async def test_disabled_music_track_missing_from_catalog_is_ignored(
    clip_two_scenes: Path, tmp_path: Path, assets: Path
) -> None:
    plan = _plan(captions=Captions(enabled=False), music=Music(enabled=True, track_id="ghost"))
    out = tmp_path / "final.mp4"
    await render_plan(
        source=clip_two_scenes,
        plan=plan,
        transcript=TRANSCRIPT,
        workdir=tmp_path / "w",
        out_path=out,
        assets_dir=assets,
        max_short_side=360,
    )
    assert (await probe(str(out))).has_audio


async def test_multi_source_broll_clip_renders_with_dubbed_primary_audio(
    clip_two_scenes: Path, clip_no_audio: Path, tmp_path: Path, assets: Path
) -> None:
    """P13 end-to-end: `sources` resolves each clip's own `source_id`, and a B-roll clip's silent-on-its-
    own-file video gets the primary's audio dubbed in underneath (real ffmpeg, both stages together)."""
    plan = make_plan([(0.0, 1.0)], captions=Captions(enabled=False))
    broll = plan.clips[0].model_copy(
        update={
            "id": "c2",
            "source_id": "broll_1",
            "src_in": 0.0,
            "src_out": 1.0,
            "role": "broll",
            "audio": ClipAudio(source="primary", primary_src_in=1.0, primary_src_out=2.0),
        }
    )
    plan = plan.model_copy(update={"clips": [plan.clips[0], broll]})
    out, workdir = tmp_path / "final.mp4", tmp_path / "w"
    stats = await render_plan(
        sources={"primary": clip_two_scenes, "broll_1": clip_no_audio},
        plan=plan,
        transcript=TRANSCRIPT,
        workdir=workdir,
        out_path=out,
        assets_dir=assets,
        max_short_side=360,
    )
    info = await probe(str(out))
    assert info.has_audio  # broll_1 (clip_no_audio) has no track of its own - this can only be the dub
    assert info.duration_sec == pytest.approx(2.0, abs=0.3) == stats.duration_sec


async def test_portrait_phone_video_stored_landscape_keeps_a_portrait_original_aspect(
    clip_two_scenes: Path, tmp_path: Path, assets: Path
) -> None:
    """Regression: phones store landscape pixels + a rotation flag. Sizing "original" output from the STORED
    size made a portrait video render landscape (and crop it badly)."""
    phone = tmp_path / "phone.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-display_rotation:v:0", "90", "-i", str(clip_two_scenes),
         "-c", "copy", str(phone)],
        check=True,
    )  # fmt: skip
    out = tmp_path / "final.mp4"
    await render_plan(
        source=phone, plan=make_plan([(0.0, 2.0)], aspect="original", captions=Captions(enabled=False)),
        transcript=TRANSCRIPT, workdir=tmp_path / "w", out_path=out, assets_dir=assets, max_short_side=360,
    )  # fmt: skip
    info = await probe(str(out))
    assert (info.width, info.height) == (360, 640)


async def test_hdr_source_is_tone_mapped_to_bt709_sdr(tmp_path: Path, assets: Path) -> None:
    hdr = tmp_path / "hdr.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-vf", "format=yuv420p10le",
         "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "high10",
         "-x264-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc", "-c:a", "aac",
         str(hdr)],
        check=True,
    )  # fmt: skip
    assert (await probe(str(hdr))).is_hdr
    out = tmp_path / "final.mp4"
    await render_plan(
        source=hdr, plan=make_plan([(0.0, 1.5)], captions=Captions(enabled=False)), transcript=TRANSCRIPT,
        workdir=tmp_path / "w", out_path=out, assets_dir=assets, max_short_side=360,
    )  # fmt: skip
    tags = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=pix_fmt,color_transfer,color_primaries", "-of", "default=nw=1", str(out)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    assert "pix_fmt=yuv420p" in tags and "color_transfer=bt709" in tags and "color_primaries=bt709" in tags


async def test_source_without_audio(clip_no_audio: Path, tmp_path: Path, assets: Path) -> None:
    plan = make_plan([(0.0, 2.0)], captions=Captions(enabled=False))
    out = tmp_path / "final.mp4"
    await render_plan(
        source=clip_no_audio,
        plan=plan,
        transcript=make_transcript([]),
        workdir=tmp_path / "w",
        out_path=out,
        assets_dir=assets,
        max_short_side=360,
    )
    assert (await probe(str(out))).has_audio


@pytest.mark.parametrize("concurrency", [1, 3])
async def test_clip_concurrency_setting(
    clip_two_scenes: Path, tmp_path: Path, assets: Path, concurrency: int
) -> None:
    plan = make_plan([(0, 1), (1, 2), (2, 3), (3, 4)], captions=Captions(enabled=False))
    out = tmp_path / "final.mp4"
    await render_plan(
        source=clip_two_scenes,
        plan=plan,
        transcript=TRANSCRIPT,
        workdir=tmp_path / "w",
        out_path=out,
        assets_dir=assets,
        max_short_side=360,
        clip_concurrency=concurrency,
    )
    assert (await probe(str(out))).duration_sec == pytest.approx(4.0, abs=0.4)


async def test_temp_files_are_removed_when_a_clip_fails(
    clip_two_scenes: Path, tmp_path: Path, assets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("clip failed")

    monkeypatch.setattr(engine, "render_clip", boom)
    workdir = tmp_path / "w"
    with pytest.raises(RuntimeError, match="clip failed"):
        await render_plan(
            source=clip_two_scenes,
            plan=_plan(),
            transcript=TRANSCRIPT,
            workdir=workdir,
            out_path=tmp_path / "f.mp4",
            assets_dir=assets,
            max_short_side=360,
        )
    assert not (workdir / "clips").exists()


async def test_output_duration_sanity_check(
    clip_two_scenes: Path, clip_no_audio: Path, tmp_path: Path, assets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def wrong_length(*, out_path: Path, **_kw: object) -> None:
        out_path.write_bytes(clip_no_audio.read_bytes())  # a 3 s file for a 5.5 s plan

    monkeypatch.setattr(engine, "render_final", wrong_length)
    plan = make_plan([(0.0, 5.5)], captions=Captions(enabled=False))
    with pytest.raises(RenderError):
        await render_plan(
            source=clip_two_scenes,
            plan=plan,
            transcript=TRANSCRIPT,
            workdir=tmp_path / "w",
            out_path=tmp_path / "f.mp4",
            assets_dir=assets,
            max_short_side=360,
        )
