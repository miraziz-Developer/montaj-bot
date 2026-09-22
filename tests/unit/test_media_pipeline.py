import json
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models import Job, Upload
from app.services.media import pipeline
from app.services.media.pipeline import run_pre_analysis
from app.services.media.probe import probe
from app.services.stt.base import Transcript, TranscriptSegment, Word
from app.services.stt.fake import FakeSTTProvider
from tests.fakes.storage import FakeBlobStorage


def _speech(text: str, start: float, end: float) -> Transcript:
    return Transcript(
        language="uz",
        segments=[
            TranscriptSegment(start=start, end=end, text=text, words=[Word(text=text, start=start, end=end)])
        ],
    )


async def _setup(clip: Path, *, has_audio: bool = True) -> tuple[Job, Upload, FakeBlobStorage, Settings]:
    info = await probe(str(clip))
    user_id, upload_id = uuid.uuid4(), uuid.uuid4()
    upload = Upload(
        id=upload_id,
        user_id=user_id,
        blob_path=f"{user_id}/{upload_id}/source.mp4",
        original_filename="clip.mp4",
        content_type="video/mp4",
        size_bytes=clip.stat().st_size,
        duration_sec=info.duration_sec,
        width=info.width,
        height=info.height,
        has_audio=has_audio,
    )
    job = Job(id=uuid.uuid4(), user_id=user_id, upload_id=upload_id)
    storage = FakeBlobStorage()
    storage.put("uploads", upload.blob_path, clip.read_bytes())
    return job, upload, storage, Settings(_env_file=None, stt_concurrency=2)


async def test_end_to_end_produces_and_uploads_all_artifacts(clip_two_scenes: Path, tmp_path: Path) -> None:
    job, upload, storage, settings = await _setup(clip_two_scenes)
    stt = FakeSTTProvider(
        by_name={"audio_000.ogg": _speech("salom", 0.5, 1.5), "audio_001.ogg": _speech("dunyo", 0.5, 1.5)}
    )
    result = await run_pre_analysis(job, upload, tmp_path / "work", storage, stt, settings, audio_chunk_sec=3)

    assert result.proxy_path.exists()
    assert [(c.start_sec, c.end_sec) for c in result.chunks] == [(0, 3), (3, 6)]
    # chunk transcripts are placed on the video timeline (second chunk shifted by 3 s)
    assert [(w.text, w.start) for w in result.transcript.all_words()] == [("salom", 0.5), ("dunyo", 3.5)]
    assert len(result.scenes) >= 2 and result.scenes[0].scene_id == 1
    assert result.silences == []  # continuous tone
    assert sorted(stt.calls) == [("audio_000.ogg", None), ("audio_001.ogg", None)]

    for name in ("proxy.mp4", "transcript.json", "scenes.json", "silences.json"):
        assert ("artifacts", f"{job.id}/{name}") in storage.blobs, name
    stored = json.loads(storage.blobs[("artifacts", f"{job.id}/transcript.json")])
    assert stored["segments"][1]["words"][0]["start"] == 3.5
    scenes_json = json.loads(storage.blobs[("artifacts", f"{job.id}/scenes.json")])
    assert scenes_json[0] == {"scene_id": 1, "start": 0.0, "end": scenes_json[0]["end"]}


async def test_rerun_reuses_stored_artifacts_instead_of_recomputing(
    clip_two_scenes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, upload, storage, settings = await _setup(clip_two_scenes)
    stt = FakeSTTProvider(default=_speech("salom", 0.5, 1.5))
    first = await run_pre_analysis(job, upload, tmp_path / "w1", storage, stt, settings, audio_chunk_sec=3)

    async def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("must not recompute")

    for name in ("make_proxy", "extract_audio_chunks", "detect_silences", "detect_scenes"):
        monkeypatch.setattr(pipeline, name, boom)
    stt_again = FakeSTTProvider()

    second = await run_pre_analysis(
        job, upload, tmp_path / "w2", storage, stt_again, settings, audio_chunk_sec=3
    )
    assert stt_again.calls == []
    assert second.transcript == first.transcript
    assert second.silences == first.silences and second.scenes == first.scenes
    assert second.chunks == [] and second.proxy_path.exists()


async def test_video_without_audio_skips_stt_and_silence(clip_no_audio: Path, tmp_path: Path) -> None:
    job, upload, storage, settings = await _setup(clip_no_audio, has_audio=False)
    stt = FakeSTTProvider(default=_speech("never", 0, 1))
    result = await run_pre_analysis(job, upload, tmp_path / "work", storage, stt, settings)
    assert stt.calls == [] and result.chunks == []
    assert result.transcript.segments == [] and result.silences == []
    assert len(result.scenes) >= 1
    assert ("artifacts", f"{job.id}/transcript.json") in storage.blobs


async def test_long_video_uses_the_480_proxy(
    clip_two_scenes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    real = pipeline.make_proxy

    async def spy(source: Path, dest: Path, *, short_side: int, **kw: object) -> None:
        seen.append(short_side)
        await real(source, dest, short_side=short_side, **kw)

    monkeypatch.setattr(pipeline, "make_proxy", spy)
    job, upload, storage, settings = await _setup(clip_two_scenes)
    long_settings = settings.model_copy(update={"long_video_threshold_sec": 1})
    await run_pre_analysis(job, upload, tmp_path / "a", storage, FakeSTTProvider(), long_settings)
    job2, upload2, storage2, _ = await _setup(clip_two_scenes)
    await run_pre_analysis(job2, upload2, tmp_path / "b", storage2, FakeSTTProvider(), settings)
    assert seen == [480, 720]


async def test_language_hint_is_forwarded(clip_two_scenes: Path, tmp_path: Path) -> None:
    job, upload, storage, settings = await _setup(clip_two_scenes)
    stt = FakeSTTProvider()
    await run_pre_analysis(
        job, upload, tmp_path / "w", storage, stt, settings, audio_chunk_sec=6, language_hint="uz"
    )
    assert stt.calls == [("audio_000.ogg", "uz")]
