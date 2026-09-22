import pytest

from app.core.errors import NotFound
from app.services.ai.plan_validator import apply_rhythm
from app.services.ai.presets import PRESETS, get_preset, preset_rules_dict
from app.services.ai.snap import snap_cuts, snap_point
from app.services.media.silence import Silence
from app.services.stt.base import Transcript
from tests.fakes.gemini import make_plan, make_transcript, speech

EMPTY = Transcript(language="uz", segments=[])


# ---------- presets ----------


def test_preset_table_matches_the_spec() -> None:
    reels, talk, ad, vlog = (
        PRESETS[k] for k in ("dynamic_reels", "clean_talk", "ad_commercial", "vlog_story")
    )
    assert (reels.max_shot_sec, reels.zoom_levels, reels.remove_gap_sec, reels.pad_sec) == (
        5,
        (1.0, 1.15),
        0.30,
        0.08,
    )
    assert (reels.captions_style, reels.captions_max_words, reels.music_volume) == ("word_highlight", 3, 0.10)
    assert (reels.target_min_sec, reels.target_max_sec) == (15, 60)
    assert (talk.max_shot_sec, talk.zoom_levels, talk.remove_gap_sec, talk.captions_style) == (
        12,
        (1.0, 1.05),
        0.50,
        "classic",
    )
    assert (ad.max_shot_sec, ad.zoom_levels, ad.music_volume, ad.target_max_sec) == (4, (1.0, 1.10), 0.14, 45)
    assert (vlog.max_shot_sec, vlog.pad_sec, vlog.captions_max_words, vlog.keep_chronology) == (
        8,
        0.12,
        4,
        True,
    )
    assert not any(p.keep_chronology for p in (reels, talk, ad))


def test_preset_rules_dict_has_the_prompt_shape() -> None:
    data = preset_rules_dict(get_preset("dynamic_reels"))
    assert data["captions"] == {
        "style": "word_highlight",
        "max_words_per_line": 3,
        "highlight_color": "#FFD400",
    }
    assert {
        "max_shot_sec",
        "remove_gap_sec",
        "pad_sec",
        "target_min_sec",
        "target_max_sec",
        "music_volume",
    } <= set(data)


def test_unknown_preset_is_not_found() -> None:
    with pytest.raises(NotFound):
        get_preset("nope")


# ---------- snap_point ----------


def test_cut_inside_a_short_silence_moves_to_its_middle() -> None:
    assert snap_point(10.2, "in", [], [Silence(10.0, 10.8)]) == pytest.approx(10.4)
    assert snap_point(10.6, "out", [], [Silence(10.0, 10.8)]) == pytest.approx(10.4)


def test_cut_inside_a_word_moves_to_the_word_edge() -> None:
    words = make_transcript([("salom", 5.0, 5.6)]).all_words()
    assert snap_point(5.3, "in", words, []) == pytest.approx(4.95)  # word.start - 0.05
    assert snap_point(5.3, "out", words, []) == pytest.approx(5.68)  # word.end + 0.08


def test_cut_near_a_word_edge_snaps_within_the_window_only() -> None:
    words = make_transcript([("a", 5.0, 5.4)]).all_words()
    assert snap_point(5.15 - 0.3, "in", words, []) == pytest.approx(4.95)  # 0.1 s from the edge
    assert snap_point(4.0, "in", words, []) == 4.0  # 0.95 s away: untouched


def test_silence_beats_word_boundary_but_never_lands_inside_a_word() -> None:
    words = make_transcript([("a", 5.0, 5.5), ("b", 5.9, 6.4)]).all_words()
    assert snap_point(5.6, "out", words, [Silence(5.5, 5.9)]) == pytest.approx(5.7)
    # a silence midpoint that STT says is inside a word falls back to that word's edge
    words = make_transcript([("x", 5.0, 6.0)]).all_words()
    assert snap_point(5.9, "out", words, [Silence(5.4, 5.8)]) == pytest.approx(6.08)


# ---------- snap_cuts ----------


def test_snap_cuts_moves_both_ends_of_a_clip() -> None:
    transcript = make_transcript([("a", 1.0, 1.5), ("b", 1.6, 2.4), ("c", 3.0, 3.5)])
    plan = snap_cuts(make_plan([(1.9, 3.3)]), transcript, [])
    assert plan.clips[0].src_in == pytest.approx(1.55)  # inside word b -> its start - 0.05
    assert plan.clips[0].src_out == pytest.approx(3.58)  # inside word c -> its end + 0.08


def test_snap_cuts_never_makes_a_clip_shorter_than_0_3() -> None:
    plan = make_plan([(10.0, 10.4)])
    snapped = snap_cuts(plan, EMPTY, [Silence(10.1, 10.3)])  # both ends would collapse to 10.2
    assert (snapped.clips[0].src_in, snapped.clips[0].src_out) == (10.0, 10.4)


def test_snap_cuts_undoes_a_new_overlap_between_neighbours() -> None:
    transcript = make_transcript([("mid", 4.95, 5.2)])
    plan = make_plan([(0.0, 5.0), (5.1, 9.0)])  # 5.0 and 5.1 are both inside the word
    snapped = snap_cuts(plan, transcript, [])
    assert [(c.src_in, c.src_out) for c in snapped.clips] == [(0.0, 5.0), (5.1, 9.0)]


def test_snap_cuts_is_pure() -> None:
    plan = make_plan([(1.9, 3.3)])
    snap_cuts(plan, make_transcript([("b", 1.6, 2.4)]), [])
    assert (plan.clips[0].src_in, plan.clips[0].src_out) == (1.9, 3.3)


# ---------- apply_rhythm ----------


def test_long_clip_is_split_at_the_pauses_and_ids_are_sequential() -> None:
    words = [*speech(0, 4.4), *speech(4.9, 9.0), *speech(9.6, 12.0)]  # pauses near 4.65 and 9.3
    transcript = make_transcript(words)
    plan = apply_rhythm(make_plan([(0.0, 12.0)]), transcript, get_preset("dynamic_reels"))
    spans = [(c.src_in, c.src_out) for c in plan.clips]
    assert len(spans) == 3
    assert all(b - a <= 5.0 for a, b in spans)
    assert spans[0][0] == 0.0 and spans[-1][1] == 12.0
    assert all(x[1] == y[0] for x, y in zip(spans, spans[1:], strict=False))  # contiguous
    assert spans[0][1] == pytest.approx(4.65, abs=0.1)
    assert [c.id for c in plan.clips] == ["c1", "c2", "c3"]


def test_split_without_transcript_uses_time_and_keeps_edges() -> None:
    plan = apply_rhythm(make_plan([(0.0, 5.5)]), EMPTY, get_preset("dynamic_reels"))
    assert all(c.src_out - c.src_in >= 1.0 for c in plan.clips)
    assert all(c.out_duration <= 5.0 + 1e-6 for c in plan.clips)


def test_split_never_cuts_through_a_word_when_a_word_sits_on_the_target() -> None:
    transcript = make_transcript([("uzun", 3.5, 5.5), ("soz", 5.6, 6.0)], segment_gap=5)
    plan = apply_rhythm(make_plan([(0.0, 9.0)]), transcript, get_preset("dynamic_reels"))
    cut = plan.clips[0].src_out
    assert not (3.5 < cut < 5.5)


def test_speed_counts_towards_max_shot() -> None:
    fast = make_plan([(0.0, 10.0)]).model_copy(deep=True)
    fast.clips[0].speed = 2.0  # 10 s of source play as 5 s: not longer than max_shot
    assert len(apply_rhythm(fast, EMPTY, get_preset("dynamic_reels")).clips) == 1


def test_zoom_alternates_through_the_preset_levels() -> None:
    plan = apply_rhythm(make_plan([(0, 4), (5, 9), (10, 14)]), EMPTY, get_preset("dynamic_reels"))
    assert [c.reframe.zoom for c in plan.clips] == [1.0, 1.15, 1.0]


def test_zoom_is_skipped_for_calm_presets_on_16_9() -> None:
    plan = apply_rhythm(make_plan([(0, 4), (5, 9)], aspect="16:9"), EMPTY, get_preset("clean_talk"))
    assert [c.reframe.zoom for c in plan.clips] == [1.0, 1.0]
    zoomed = apply_rhythm(make_plan([(0, 4), (5, 9)], aspect="9:16"), EMPTY, get_preset("clean_talk"))
    assert [c.reframe.zoom for c in zoomed.clips] == [1.0, 1.05]


def test_hook_role_stays_on_the_first_piece_only() -> None:
    plan = make_plan([(0.0, 9.0)])
    plan.clips[0].role = "hook"
    result = apply_rhythm(plan, EMPTY, get_preset("dynamic_reels"))
    assert [c.role for c in result.clips][0] == "hook" and all(c.role == "body" for c in result.clips[1:])


def test_ids_are_renumbered_without_gaps_or_duplicates() -> None:
    plan = make_plan([(0, 3), (4, 7)])
    plan.clips[0].id, plan.clips[1].id = "x9", "x3"
    assert [c.id for c in apply_rhythm(plan, EMPTY, get_preset("dynamic_reels")).clips] == ["c1", "c2"]
