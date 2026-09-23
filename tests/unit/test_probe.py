import json
import subprocess
from pathlib import Path

import pytest

from app.core.errors import InvalidMedia
from app.services.media.ffmpeg import FFmpegError, redact_urls, run
from app.services.media.probe import parse_probe_output, probe


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("media")
    video = root / "with_audio.mp4"
    silent = root / "silent.mp4"
    audio = root / "audio_only.m4a"
    garbage = root / "garbage.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x360:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440",
        "-t",
        "3",
        "-pix_fmt",
        "yuv420p",
        str(video),
    )
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-t", "2", "-pix_fmt", "yuv420p", str(silent)
    )
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", str(audio))
    garbage.write_bytes(b"this is not a video" * 100)
    return {"video": video, "silent": silent, "audio": audio, "garbage": garbage}


async def test_probe_real_video(media: dict[str, Path]) -> None:
    result = await probe(str(media["video"]))
    assert (result.width, result.height) == (640, 360)
    assert result.duration_sec == pytest.approx(3.0, abs=0.3)
    assert result.fps == pytest.approx(30.0)
    assert result.has_audio is True
    assert result.video_codec == "h264"
    assert result.size_bytes == media["video"].stat().st_size


async def test_probe_video_without_audio(media: dict[str, Path]) -> None:
    result = await probe(str(media["silent"]))
    assert result.has_audio is False
    assert (result.width, result.height) == (320, 240)


@pytest.mark.parametrize("name", ["audio", "garbage"])
async def test_probe_rejects_non_video(media: dict[str, Path], name: str) -> None:
    with pytest.raises(InvalidMedia):
        await probe(str(media[name]))


async def test_probe_missing_file_is_invalid_media(tmp_path: Path) -> None:
    with pytest.raises(InvalidMedia):
        await probe(str(tmp_path / "nope.mp4"))


def _payload(**overrides: object) -> str:
    data: dict = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 10,
                "height": 20,
                "avg_frame_rate": "30000/1001",
            }
        ],
        "format": {"duration": "12.5", "size": "999", "format_name": "mp4"},
    }
    data.update(overrides)
    return json.dumps(data)


def test_parse_ntsc_fps_and_fields() -> None:
    result = parse_probe_output(_payload())
    assert result.fps == pytest.approx(29.97, abs=0.01)
    assert (result.duration_sec, result.size_bytes, result.format_name) == (12.5, 999, "mp4")


def _video(**fields: object) -> str:
    return _payload(streams=[{"codec_type": "video", "width": 1920, "height": 1080, **fields}])


@pytest.mark.parametrize(
    ("fields", "size", "rotation"),
    [
        ({}, (1920, 1080), 0),
        ({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}, (1080, 1920), 270),
        ({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}]}, (1080, 1920), 90),
        ({"side_data_list": [{"rotation": 180}]}, (1920, 1080), 180),  # upside down: size unchanged
        ({"tags": {"rotate": "90"}}, (1080, 1920), 90),  # older files use a tag instead of side data
        ({"tags": {"rotate": "garbage"}}, (1920, 1080), 0),
    ],
)
def test_parse_reports_display_size_for_rotated_phone_video(
    fields: dict, size: tuple[int, int], rotation: int
) -> None:
    result = parse_probe_output(_video(**fields))
    assert ((result.width, result.height), result.rotation) == (size, rotation)


@pytest.mark.parametrize(
    ("transfer", "hdr"),
    [("arib-std-b67", True), ("smpte2084", True), ("bt709", False), (None, False)],
)
def test_parse_flags_hdr_transfer_characteristics(transfer: str | None, hdr: bool) -> None:
    fields = {} if transfer is None else {"color_transfer": transfer}
    assert parse_probe_output(_video(**fields)).is_hdr is hdr


async def test_probe_real_rotated_file_reports_display_size(tmp_path: Path) -> None:
    stored, rotated = tmp_path / "stored.mp4", tmp_path / "rotated.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30", "-t", "1", "-pix_fmt", "yuv420p", str(stored)
    )
    _ffmpeg("-display_rotation:v:0", "90", "-i", str(stored), "-c", "copy", str(rotated))
    result = await probe(str(rotated))
    assert (result.width, result.height, result.rotation) == (360, 640, 90)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        _payload(streams=[]),
        _payload(format={"duration": "0"}),
        _payload(format={"duration": "nan"}),
        _payload(format={}),
        _payload(
            streams=[{"codec_type": "video", "width": 10, "height": 20, "disposition": {"attached_pic": 1}}]
        ),
        _payload(streams=[{"codec_type": "video", "codec_name": "h264"}]),
    ],
)
def test_parse_rejects_unusable_output(payload: str) -> None:
    with pytest.raises(InvalidMedia):
        parse_probe_output(payload)


def test_duration_falls_back_to_stream() -> None:
    payload = _payload(
        streams=[{"codec_type": "video", "width": 4, "height": 4, "duration": "7.0"}], format={}
    )
    assert parse_probe_output(payload).duration_sec == 7.0


async def test_run_success() -> None:
    code, out, _ = await run(["echo", "hi"], timeout=5)
    assert (code, out.strip()) == (0, "hi")


async def test_run_failure_keeps_stderr_tail_and_redacts_sas() -> None:
    script = "echo 'boom https://blob.example/x?sig=SECRET&sp=r' >&2; exit 3"
    with pytest.raises(FFmpegError) as exc:
        await run(["sh", "-c", script], timeout=5)
    message = str(exc.value)
    assert "code 3" in message and "boom" in message
    assert "SECRET" not in message


async def test_run_error_message_is_capped() -> None:
    with pytest.raises(FFmpegError) as exc:
        await run(["sh", "-c", "head -c 10000 /dev/zero | tr '\\0' 'x' >&2; exit 1"], timeout=5)
    assert len(str(exc.value)) < 2100


async def test_run_timeout_kills_process() -> None:
    with pytest.raises(FFmpegError, match="timed out"):
        await run(["sleep", "5"], timeout=0.2)


async def test_run_missing_binary() -> None:
    with pytest.raises(FFmpegError, match="could not start"):
        await run(["definitely-not-a-binary"], timeout=1)


async def test_run_check_false_returns_code() -> None:
    code, _, _ = await run(["sh", "-c", "exit 2"], timeout=5, check=False)
    assert code == 2


def test_redact_urls() -> None:
    assert redact_urls("open http://a.b/c?sig=1&x=2 failed") == "open http://a.b/c?<redacted> failed"
