from pathlib import Path

import pytest

from app.services.media import audio as audio_module
from app.services.media.audio import chunk_ranges, extract_audio_chunks
from app.services.media.ffmpeg import FFmpegError, run_ffprobe
from app.services.media.probe import media_duration, probe
from app.services.media.proxy import make_proxy


async def test_proxy_short_side_landscape(clip_two_scenes: Path, tmp_path: Path) -> None:
    dest = tmp_path / "proxy.mp4"
    await make_proxy(clip_two_scenes, dest, short_side=240)
    info = await probe(str(dest))
    assert info.height == 240
    assert info.width % 2 == 0 and abs(info.width - 427) <= 1
    assert info.video_codec == "h264" and info.has_audio
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no temp leftovers


async def test_proxy_short_side_portrait(clip_portrait: Path, tmp_path: Path) -> None:
    dest = tmp_path / "proxy.mp4"
    await make_proxy(clip_portrait, dest, short_side=240)
    info = await probe(str(dest))
    assert info.width == 240 and info.height % 2 == 0 and abs(info.height - 427) <= 1


async def test_proxy_never_upscales(clip_two_scenes: Path, tmp_path: Path) -> None:
    await make_proxy(clip_two_scenes, tmp_path / "p.mp4", short_side=720)
    info = await probe(str(tmp_path / "p.mp4"))
    assert (info.width, info.height) == (640, 360)


async def test_proxy_source_without_audio(clip_no_audio: Path, tmp_path: Path) -> None:
    await make_proxy(clip_no_audio, tmp_path / "p.mp4", short_side=240)
    assert (await probe(str(tmp_path / "p.mp4"))).has_audio is False


async def test_proxy_keyframe_interval_and_faststart_args(clip_two_scenes: Path, tmp_path: Path) -> None:
    dest = tmp_path / "p.mp4"
    await make_proxy(clip_two_scenes, dest, short_side=240, fps=30)
    _, out, _ = await run_ffprobe(
        [
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(dest),
        ]
    )
    keyframes = [float(x.strip(",")) for x in out.split() if x.strip(",")]
    assert keyframes[0] == 0.0 and all(b - a <= 2.05 for a, b in zip(keyframes, keyframes[1:], strict=False))


async def test_proxy_failure_leaves_no_destination(tmp_path: Path) -> None:
    dest = tmp_path / "p.mp4"
    with pytest.raises(FFmpegError):
        await make_proxy(tmp_path / "missing.mp4", dest, short_side=240)
    assert not dest.exists()
    assert not [p for p in tmp_path.iterdir()]


# ---------- audio chunks ----------


@pytest.mark.parametrize(
    ("duration", "chunk", "expected"),
    [
        (6.0, 2, [(0, 2), (2, 4), (4, 6)]),
        (5.0, 2, [(0, 2), (2, 4), (4, 5)]),
        (6.1, 2, [(0, 2), (2, 4), (4, 6)]),  # 0.1 s sliver dropped
        (1.5, 600, [(0, 1.5)]),
        (0.1, 600, []),
    ],
)
def test_chunk_ranges(duration: float, chunk: int, expected: list[tuple[float, float]]) -> None:
    assert chunk_ranges(duration, chunk) == expected


async def test_no_audio_returns_empty_without_calling_ffmpeg(
    clip_no_audio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("ffmpeg must not be called")

    monkeypatch.setattr(audio_module, "run_ffmpeg", boom)
    assert await extract_audio_chunks(clip_no_audio, tmp_path / "a", chunk_sec=2) == []
    assert (
        await extract_audio_chunks(
            clip_no_audio, tmp_path / "a", chunk_sec=2, has_audio=False, duration_sec=3
        )
        == []
    )


async def test_chunks_cover_the_duration_and_are_mono_opus(clip_two_scenes: Path, tmp_path: Path) -> None:
    chunks = await extract_audio_chunks(clip_two_scenes, tmp_path / "a", chunk_sec=2)
    assert [(c.start_sec, c.end_sec) for c in chunks] == [(0, 2), (2, 4), (4, 6)]
    assert [c.path.name for c in chunks] == ["audio_000.ogg", "audio_001.ogg", "audio_002.ogg"]
    for chunk in chunks:
        assert await media_duration(chunk.path) == pytest.approx(2.0, abs=0.15)
    _, out, _ = await run_ffprobe(
        [
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,channels,sample_rate",
            "-of",
            "csv=p=0",
            str(chunks[0].path),
        ]
    )
    # Ogg/Opus always reports 48 kHz; what matters is opus + mono (input was resampled to 16 kHz)
    assert out.strip() == "opus,48000,1"


async def test_existing_chunks_are_reused(
    clip_two_scenes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await extract_audio_chunks(
        clip_two_scenes, tmp_path / "a", chunk_sec=3, has_audio=True, duration_sec=6
    )

    async def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("chunks already exist")

    monkeypatch.setattr(audio_module, "run_ffmpeg", boom)
    second = await extract_audio_chunks(
        clip_two_scenes, tmp_path / "a", chunk_sec=3, has_audio=True, duration_sec=6
    )
    assert first == second
