from pathlib import Path

import pytest

from app.services.media.probe import media_duration
from app.services.media.scenes import Scene, detect_scenes, postprocess_scenes
from app.services.media.silence import Silence, detect_silences, parse_silencedetect

# ---------- silencedetect parsing ----------

SAMPLE = """
[silencedetect @ 0x1] silence_start: 2.0021
[silencedetect @ 0x1] silence_end: 3.0004 | silence_duration: 0.998
[silencedetect @ 0x1] silence_start: 5.5
"""


def test_parse_pairs_and_open_silence_closed_at_eof() -> None:
    assert parse_silencedetect(SAMPLE, duration=6.0) == [Silence(2.002, 3.0), Silence(5.5, 6.0)]


def test_parse_clamps_negative_start_and_ignores_stray_end() -> None:
    text = (
        "silence_end: 1.0 | silence_duration: 1\n"
        "silence_start: -0.02\n"
        "silence_end: 0.6 | silence_duration: 0.6"
    )
    assert parse_silencedetect(text, duration=6.0) == [Silence(0.0, 0.6)]


def test_parse_nothing() -> None:
    assert parse_silencedetect("size=N/A time=00:00:06", duration=6.0) == []


# ---------- silence on real audio ----------


async def test_detects_inserted_gap(clip_silent_middle: Path) -> None:
    silences = await detect_silences(clip_silent_middle)
    assert len(silences) == 1
    assert silences[0].start == pytest.approx(2.0, abs=0.2)
    assert silences[0].end == pytest.approx(3.0, abs=0.2)


async def test_continuous_tone_has_no_silence(clip_continuous: Path) -> None:
    assert await detect_silences(clip_continuous) == []


async def test_silence_open_at_eof_is_closed_at_duration(clip_silent_tail: Path) -> None:
    silences = await detect_silences(clip_silent_tail)
    assert len(silences) == 1
    assert silences[0].start == pytest.approx(4.5, abs=0.2)
    assert silences[0].end == pytest.approx(await media_duration(clip_silent_tail), abs=0.01)


# ---------- scene post-processing (pure) ----------


def _spans(scenes: list[Scene]) -> list[tuple[float, float]]:
    return [(s.start, s.end) for s in scenes]


def test_no_cuts_gives_one_scene() -> None:
    assert postprocess_scenes([], 9.0, [], 1.0) == [Scene(1, 0.0, 9.0)]


def test_sliver_merges_into_next_scene() -> None:
    scenes = postprocess_scenes([4.0, 4.5], 10.0, [], 1.0)  # 4.0-4.5 is a 0.5 s sliver
    assert _spans(scenes) == [(0.0, 4.0), (4.0, 10.0)]
    assert [s.scene_id for s in scenes] == [1, 2]


def test_short_last_scene_merges_into_previous() -> None:
    assert _spans(postprocess_scenes([5.0, 9.5], 10.0, [], 1.0)) == [(0.0, 5.0), (5.0, 10.0)]


def test_first_scene_sliver_merges_forward() -> None:
    assert _spans(postprocess_scenes([0.4], 10.0, [], 1.0)) == [(0.0, 10.0)]


def test_video_shorter_than_min_len_stays_one_scene() -> None:
    assert postprocess_scenes([], 0.6, [], 1.0) == [Scene(1, 0.0, 0.6)]


def test_long_scene_splits_at_8s_boundaries_without_silence() -> None:
    assert _spans(postprocess_scenes([], 20.0, [], 1.0)) == [(0.0, 8.0), (8.0, 20.0)]
    assert _spans(postprocess_scenes([], 20.5, [], 1.0)) == [(0.0, 8.0), (8.0, 16.0), (16.0, 20.5)]


def test_long_scene_prefers_silence_midpoint() -> None:
    silences = [Silence(9.0, 10.0), Silence(3.0, 3.4)]  # midpoints 9.5 and 3.2; 9.5 is nearer to 8 s
    scenes = postprocess_scenes([], 20.0, silences, 1.0)
    assert _spans(scenes)[0] == (0.0, 9.5)


def test_split_never_creates_a_piece_shorter_than_min_len() -> None:
    for duration in (12.4, 12.9, 16.5, 24.2):
        scenes = postprocess_scenes([], duration, [], 1.0)
        assert all(s.end - s.start >= 1.0 for s in scenes), duration
        assert all(s.end - s.start <= 12.0 for s in scenes), duration


def test_scenes_are_contiguous_renumbered_and_cover_everything() -> None:
    scenes = postprocess_scenes([2.0, 2.3, 15.0, 30.0], 40.0, [Silence(7, 8)], 1.0)
    assert [s.scene_id for s in scenes] == list(range(1, len(scenes) + 1))
    assert scenes[0].start == 0.0 and scenes[-1].end == 40.0
    assert all(a.end == b.start for a, b in zip(scenes, scenes[1:], strict=False))
    assert all(1.0 <= s.end - s.start <= 12.0 for s in scenes)


# ---------- scene detection on real video ----------


async def test_detects_the_cut_between_two_different_halves(clip_two_scenes: Path) -> None:
    scenes = await detect_scenes(clip_two_scenes)
    assert len(scenes) >= 2
    assert any(abs(s.start - 3.0) <= 0.2 for s in scenes)  # a boundary at the hard cut
    assert scenes[0].start == 0.0 and scenes[-1].end == pytest.approx(6.0, abs=0.1)


async def test_static_video_is_split_by_the_12s_rule(clip_static_20s: Path) -> None:
    scenes = await detect_scenes(clip_static_20s)
    assert len(scenes) >= 2
    assert all(s.end - s.start <= 12.0 for s in scenes)
    assert scenes[-1].end == pytest.approx(20.0, abs=0.1)
