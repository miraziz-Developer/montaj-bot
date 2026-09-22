from pathlib import Path

import pytest

from app.schemas.analysis import KeyMoment, OverallInfo, SceneAnalysis, VideoAnalysis
from app.services.ai.analysis import analyze_full_video, split_windows
from app.services.ai.plan_text import transcript_per_scene
from app.services.media.scenes import Scene
from tests.fakes.gemini import FakeGeminiClient, make_transcript

PROXY = Path("/tmp/proxy.mp4")


def _scenes(count: int, length: float = 10.0) -> list[Scene]:
    return [Scene(i + 1, i * length, (i + 1) * length) for i in range(count)]


async def _analyze(gemini: FakeGeminiClient, scenes: list[Scene], chunk_sec: float, **kw):  # noqa: ANN003, ANN202
    args = {"transcript": make_transcript([]), "niche": "", "purpose": ""}
    return await analyze_full_video(
        gemini, proxy_path=PROXY, scenes=scenes, chunk_sec=chunk_sec, **{**args, **kw}
    )


def test_windows_never_split_a_scene_and_respect_chunk_sec() -> None:
    windows = split_windows(_scenes(7), chunk_sec=25)  # 10 s scenes: two fit (20 s), three do not (30 s)
    assert [[s.scene_id for s in w] for w in windows] == [[1, 2], [3, 4], [5, 6], [7]]
    assert all(w[-1].end - w[0].start <= 25 for w in windows)


def test_single_window_when_everything_fits() -> None:
    assert len(split_windows(_scenes(3), chunk_sec=30)) == 1
    assert split_windows([], chunk_sec=30) == []


async def test_one_call_when_the_video_fits_one_window() -> None:
    gemini = FakeGeminiClient()
    analysis, usage = await _analyze(gemini, _scenes(3), 600)
    assert len(gemini.analyze_calls) == 1
    assert [s.scene_id for s in analysis.scenes] == [1, 2, 3] and usage.input_tokens == 100
    assert gemini.released == [PROXY]


async def test_long_video_is_analyzed_in_windows_and_merged_in_order() -> None:
    gemini = FakeGeminiClient(analysis_delay=0.01)
    analysis, usage = await _analyze(gemini, _scenes(7), 25)
    assert len(gemini.analyze_calls) == 4
    assert [s.scene_id for s in analysis.scenes] == [1, 2, 3, 4, 5, 6, 7]  # no gaps, no duplicates
    assert usage.input_tokens == 400
    windows = sorted(c["window"] for c in gemini.analyze_calls)
    assert windows == [(0.0, 20.0), (20.0, 40.0), (40.0, 60.0), (60.0, 70.0)]
    assert all(c["video_path"] == PROXY for c in gemini.analyze_calls)
    assert [m.scene_id for m in analysis.moments] == [1, 3, 5, 7]  # one moment per window's first scene


async def test_windows_get_only_their_own_scenes_and_transcript() -> None:
    transcript = make_transcript([("birinchi", 1.0, 2.0), ("ikkinchi", 21.0, 22.0)])
    gemini = FakeGeminiClient()
    await _analyze(gemini, _scenes(4), 25, transcript=transcript, niche="n", purpose="p")
    first = next(c for c in gemini.analyze_calls if c["window"][0] == 0.0)
    second = next(c for c in gemini.analyze_calls if c["window"][0] == 20.0)
    assert [s["scene_id"] for s in first["scenes"]] == [1, 2]
    assert [s["scene_id"] for s in second["scenes"]] == [3, 4]
    assert first["transcript_by_scene"][0] == {"scene_id": 1, "text": "birinchi"}
    assert second["transcript_by_scene"][0] == {"scene_id": 3, "text": "ikkinchi"}
    assert (first["niche"], first["purpose"]) == ("n", "p")


async def test_concurrency_is_bounded() -> None:
    gemini = FakeGeminiClient(analysis_delay=0.02)
    await _analyze(gemini, _scenes(8), 10, max_concurrency=2)
    assert gemini.max_running == 2


async def test_missing_extra_and_duplicate_scene_ids_are_normalized() -> None:
    def sloppy(scenes: list[dict]) -> VideoAnalysis:  # noqa: ARG001
        return VideoAnalysis(
            overall=OverallInfo(has_speech=False),
            scenes=[
                SceneAnalysis(scene_id=2, description="two"),
                SceneAnalysis(scene_id=2, description="dup"),
                SceneAnalysis(scene_id=99, description="ghost"),
            ],
            moments=[KeyMoment(scene_id=99, type="cta"), KeyMoment(scene_id=2, type="punchline")],
        )

    analysis, _ = await _analyze(FakeGeminiClient(analysis_factory=sloppy), _scenes(3), 600)
    assert [(s.scene_id, s.description) for s in analysis.scenes] == [(1, ""), (2, "two"), (3, "")]
    assert [m.scene_id for m in analysis.moments] == [2]


async def test_overall_comes_from_the_first_window_but_speech_is_the_union() -> None:
    def per_window(scenes: list[dict]) -> VideoAnalysis:
        first = scenes[0]["scene_id"] == 1
        return VideoAnalysis(
            overall=OverallInfo(summary="first" if first else "later", has_speech=not first),
            scenes=[SceneAnalysis(scene_id=s["scene_id"]) for s in scenes],
        )

    analysis, _ = await _analyze(FakeGeminiClient(analysis_factory=per_window), _scenes(4), 20)
    assert analysis.overall.summary == "first" and analysis.overall.has_speech is True


async def test_video_is_released_even_when_the_llm_fails() -> None:
    class Boom(FakeGeminiClient):
        async def analyze_video(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("llm down")

    gemini = Boom()
    with pytest.raises(RuntimeError):
        await _analyze(gemini, _scenes(2), 600)
    assert gemini.released == [PROXY]


async def test_no_scenes_means_no_calls() -> None:
    gemini = FakeGeminiClient()
    analysis, usage = await _analyze(gemini, [], 600)
    assert analysis.scenes == [] and usage.input_tokens == 0 and gemini.analyze_calls == []


def test_transcript_per_scene_uses_words_then_falls_back_to_segments_and_truncates() -> None:
    transcript = make_transcript([("salom", 0.5, 1.0), ("dunyo", 1.2, 1.8)])
    text = transcript_per_scene([Scene(1, 0.0, 2.0), Scene(2, 2.0, 4.0)], transcript)
    assert text == [{"scene_id": 1, "text": "salom dunyo"}, {"scene_id": 2, "text": ""}]
    long = make_transcript([(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(200)])
    assert len(transcript_per_scene([Scene(1, 0.0, 100.0)], long, max_chars=300)[0]["text"]) == 300
