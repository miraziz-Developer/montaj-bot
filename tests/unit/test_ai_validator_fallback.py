from app.schemas.edit_plan import Music, TextOverlay
from app.services.ai.fallback_planner import SUMMARY_TEMPLATE, build_fallback_plan
from app.services.ai.plan_validator import force_job_settings, validate_plan
from app.services.ai.presets import get_preset
from app.services.media.silence import Silence
from app.services.stt.base import Transcript
from tests.fakes.gemini import make_plan, make_transcript, speech

REELS = get_preset("dynamic_reels")
EMPTY = Transcript(language="uz", segments=[])


def _validate(plan, duration=60.0, preset=REELS, music_ids=()):  # noqa: ANN001, ANN202
    return validate_plan(plan, duration, preset, set(music_ids))


# ---------- validate_plan ----------


def test_valid_plan_passes_untouched() -> None:
    plan, errors = _validate(make_plan([(0, 10), (20, 30)]))
    assert errors == [] and [(c.src_in, c.src_out) for c in plan.clips] == [(0, 10), (20, 30)]


def test_unknown_music_track_is_disabled_without_error() -> None:
    plan = make_plan([(0, 10)], music=Music(enabled=True, track_id="ghost"))
    fixed, errors = _validate(plan, music_ids={"upbeat_01"})
    assert errors == [] and fixed.music.enabled is False and fixed.music.track_id is None


def test_known_music_track_is_kept() -> None:
    plan = make_plan([(0, 10)], music=Music(enabled=True, track_id="upbeat_01"))
    fixed, errors = _validate(plan, music_ids={"upbeat_01"})
    assert errors == [] and fixed.music.enabled and fixed.music.track_id == "upbeat_01"


def test_overlapping_clips_are_a_hard_error() -> None:
    _, errors = _validate(make_plan([(0, 10), (9, 20)]))
    assert len(errors) == 1 and "overlap" in errors[0]


def test_overlap_is_a_hard_error_for_every_preset() -> None:
    for key in ("dynamic_reels", "clean_talk", "ad_commercial", "vlog_story"):
        _, errors = _validate(make_plan([(0, 10), (5, 15)]), preset=get_preset(key))
        assert errors, key


def test_tiny_overlap_within_tolerance_is_fine() -> None:
    _, errors = _validate(make_plan([(0, 10.04), (10, 20)]))
    assert errors == []


def test_total_duration_over_105_percent_of_source_is_a_hard_error() -> None:
    plan = make_plan([(0, 30), (30, 60)])
    plan.clips[0].speed = plan.clips[1].speed = 0.5  # 120 s of output from a 60 s source
    _, errors = _validate(plan, duration=60)
    assert any("longer" in e for e in errors)


def test_slightly_out_of_range_clip_is_clamped() -> None:
    fixed, errors = _validate(make_plan([(0, 10), (50, 60.4)]), duration=60)
    assert errors == []
    assert [(c.src_in, c.src_out) for c in fixed.clips] == [(0.0, 10), (50, 60.0)]


def test_clip_entirely_outside_the_source_is_a_hard_error() -> None:
    _, errors = _validate(make_plan([(0, 10), (70, 80)]), duration=60)
    assert any("entirely outside" in e for e in errors)


def test_clamping_that_leaves_a_sliver_drops_the_clip() -> None:
    fixed, errors = _validate(make_plan([(0, 10), (59.9, 61)]), duration=60)
    assert errors == [] and len(fixed.clips) == 1


def test_no_clips_left_is_a_hard_error() -> None:
    _, errors = _validate(make_plan([(70, 80)]), duration=60)
    assert any("no usable clips" in e for e in errors)


def test_overlays_are_clamped_dropped_and_capped() -> None:
    def overlay(text: str, start: float, end: float) -> TextOverlay:
        return TextOverlay(text=text, start=start, end=end)

    plan = make_plan(
        [(0, 20)],
        overlays=[
            overlay("a", 1, 30),
            overlay("b", 19.9, 25),
            overlay("c", 25, 26),
            overlay("d", 2, 3),
            overlay("e", 4, 5),
        ],
    )
    fixed, errors = _validate(plan)
    assert errors == []
    assert [(o.text, o.end) for o in fixed.overlays] == [
        ("a", 20.0),
        ("d", 3),
        ("e", 5),
    ]  # b too short and c outside are dropped


def test_ids_are_renumbered_after_drops() -> None:
    fixed, _ = _validate(make_plan([(0, 10), (59.9, 61), (20, 30)]), duration=60)
    assert [c.id for c in fixed.clips] == ["c1", "c2"]


def test_vlog_story_clips_are_put_back_in_chronological_order() -> None:
    fixed, errors = _validate(
        make_plan([(20, 30), (0, 10)], style="vlog_story"), preset=get_preset("vlog_story")
    )
    assert errors == [] and [c.src_in for c in fixed.clips] == [0, 20]


# ---------- force_job_settings ----------


def _force(plan, **kw):  # noqa: ANN001, ANN202
    args = {"aspect": "9:16", "style_preset": "dynamic_reels", "is_trial": False, "has_words": True}
    return force_job_settings(plan, **{**args, **kw})


def test_job_decides_aspect_and_style() -> None:
    forced = _force(
        make_plan([(0, 5)], style="clean_talk", aspect="1:1"), aspect="16:9", style_preset="vlog_story"
    )
    assert (forced.target.aspect, forced.style_preset) == ("16:9", "vlog_story")


def test_trial_jobs_get_a_watermark_and_paid_jobs_cannot_have_one() -> None:
    assert _force(make_plan([(0, 5)]), is_trial=True, watermark_text="@bot").watermark.model_dump() == {
        "enabled": True, "text": "@bot",
    }  # fmt: skip
    llm_plan = make_plan([(0, 5)], watermark={"enabled": True, "text": "EVIL TEXT"})
    assert _force(llm_plan).watermark.enabled is False


def test_captions_are_disabled_without_words() -> None:
    assert _force(make_plan([(0, 5)]), has_words=False).captions.enabled is False
    assert _force(make_plan([(0, 5)]), has_words=True).captions.enabled is True


# ---------- fallback planner ----------


def _fallback(transcript, silences, duration, preset="dynamic_reels", **kw):  # noqa: ANN001, ANN202
    args = {
        "source_duration": duration, "transcript": transcript, "silences": silences, "preset": preset,
        "preset_rules": get_preset(preset), "music_tracks": [], "aspect": "9:16", "is_trial": False,
    }  # fmt: skip
    return build_fallback_plan(**{**args, **kw})


def test_five_second_silence_is_cut_out_of_the_fallback_plan() -> None:
    transcript = make_transcript([*speech(0, 10), *speech(15, 25)])
    plan = _fallback(transcript, [Silence(10.0, 15.0)], 25.0)
    spans = [(c.src_in, c.src_out) for c in plan.clips]
    assert len(spans) >= 2
    covered_in_gap = sum(max(0, min(b, 14.5) - max(a, 10.5)) for a, b in spans)
    assert covered_in_gap == 0  # (nearly) nothing of 10.5..14.5 survives
    assert spans[0][0] == 0.0 and spans[-1][1] == 25.0


def test_fallback_plan_is_valid_and_uses_preset_defaults() -> None:
    transcript = make_transcript(speech(0, 20))
    plan = _fallback(transcript, [], 20.0)
    validated, errors = _validate(plan, duration=20.0)
    assert errors == []
    assert (
        plan.captions.enabled
        and plan.captions.style == "word_highlight"
        and plan.captions.max_words_per_line == 3
    )
    assert plan.overlays == [] and plan.human_summary_uz == SUMMARY_TEMPLATE.format(
        preset_label="Dinamik Reels"
    )
    assert all(c.out_duration <= 5.0 + 1e-6 for c in validated.clips)


def test_fallback_summary_is_the_fixed_uzbek_sentence() -> None:
    plan = _fallback(make_transcript(speech(0, 5)), [], 5.0, preset="clean_talk")
    assert (
        plan.human_summary_uz
        == "Videodan ortiqcha pauzalarni olib tashladim va Sokin gap uslubida montaj qildim."
    )


def test_fallback_music_only_when_the_catalog_has_a_track() -> None:
    transcript = make_transcript(speech(0, 10))
    assert _fallback(transcript, [], 10.0).music.enabled is False
    with_music = _fallback(transcript, [], 10.0, music_tracks=[{"id": "upbeat_01", "mood": "upbeat"}])
    assert (with_music.music.enabled, with_music.music.track_id, with_music.music.volume) == (
        True,
        "upbeat_01",
        0.10,
    )


def test_fallback_without_speech_keeps_the_whole_video_without_captions() -> None:
    plan = _fallback(EMPTY, [Silence(2.0, 8.0)], 12.0)
    assert plan.captions.enabled is False
    assert plan.clips[0].src_in == 0.0 and plan.clips[-1].src_out == 12.0


def test_fallback_respects_the_target_maximum_length() -> None:
    plan = _fallback(make_transcript(speech(0, 100)), [], 100.0)  # dynamic_reels max 60 s
    assert plan.total_duration() <= 60.0 + 1e-6


def test_fallback_trial_watermark_and_aspect() -> None:
    plan = _fallback(make_transcript(speech(0, 10)), [], 10.0, is_trial=True, aspect="1:1")
    assert plan.watermark.enabled and plan.target.aspect == "1:1"


def test_fallback_drops_noise_fragments_between_long_silences() -> None:
    transcript = make_transcript(speech(0, 5) + speech(11, 16))
    plan = _fallback(transcript, [Silence(5.0, 10.0), Silence(10.4, 10.9)], 16.0)
    assert all(c.src_out - c.src_in >= 1.0 for c in plan.clips)
