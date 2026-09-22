import uuid

import pytest

from app.core.errors import AIError
from app.models import Job
from app.models.enums import PlanSource
from app.schemas.analysis import SceneAnalysis, VideoAnalysis
from app.services.ai.llm import UsageInfo
from app.services.ai.plan_text import PlanContext, SourceInfo, compact_analysis, compact_transcript
from app.services.ai.plan_validator import validate_plan
from app.services.ai.planner import REVISION_FAILED_UZ, build_initial_plan, build_revised_plan
from app.services.ai.presets import get_preset
from app.services.media.scenes import Scene
from app.services.media.silence import Silence
from tests.fakes.gemini import FakeGeminiClient, make_plan, make_transcript, speech

DURATION = 40.0


def _job(**kw) -> Job:  # noqa: ANN003
    fields = {"aspect": "9:16", "style_preset": "dynamic_reels", "is_trial": False, "brief": ""}
    return Job(id=uuid.uuid4(), user_id=uuid.uuid4(), upload_id=uuid.uuid4(), **{**fields, **kw})


def _ctx(with_speech: bool = True, music: bool = True) -> PlanContext:
    transcript = make_transcript([*speech(0, 15), *speech(16, 40)] if with_speech else [])
    scenes = [Scene(1, 0.0, 20.0), Scene(2, 20.0, DURATION)]
    return PlanContext(
        source=SourceInfo(DURATION, 1920, 1080, True, with_speech),
        analysis=VideoAnalysis(
            scenes=[SceneAnalysis(scene_id=1, highlight_score=0.9), SceneAnalysis(scene_id=2)]
        ),
        scenes=scenes,
        transcript=transcript,
        silences=[Silence(15.0, 16.0)],
        music_tracks=[{"id": "upbeat_01", "mood": "upbeat"}] if music else [],
        creator_profile={"niche": "auto", "purpose": "reels"},
    )


# ---------- build_initial_plan ----------


async def test_good_ai_plan_is_post_processed_and_marked_ai_initial() -> None:
    llm_plan = make_plan([(2.0, 12.0), (20.0, 30.0)], aspect="16:9", style="clean_talk")
    gemini = FakeGeminiClient(plan=llm_plan)
    plan, source, usage = await build_initial_plan(gemini, job=_job(), ctx=_ctx())
    assert source == PlanSource.AI_INITIAL and usage == UsageInfo(1000, 200)
    assert (plan.target.aspect, plan.style_preset) == ("9:16", "dynamic_reels")  # the job wins over the LLM
    assert all(c.out_duration <= 5.0 + 1e-6 for c in plan.clips)  # rhythm split the 10 s clips
    assert [c.id for c in plan.clips] == [f"c{i}" for i in range(1, len(plan.clips) + 1)]
    assert validate_plan(plan, DURATION, get_preset("dynamic_reels"), {"upbeat_01"})[1] == []


async def test_llm_receives_the_compact_planner_input() -> None:
    gemini = FakeGeminiClient(plan=make_plan([(2.0, 8.0)]))
    await build_initial_plan(gemini, job=_job(brief="Narxni boshida ko‘rsat"), ctx=_ctx())
    [context] = gemini.plan_contexts
    assert context["job"] == {
        "aspect": "9:16",
        "style_preset": "dynamic_reels",
        "brief": "Narxni boshida ko‘rsat",
    }
    assert context["source"]["duration_sec"] == DURATION and context["source"]["has_speech"] is True
    assert context["preset_rules"]["max_shot_sec"] == 5
    assert context["creator_profile"] == {"niche": "auto", "purpose": "reels"}
    assert context["music_tracks"] == [{"id": "upbeat_01", "mood": "upbeat"}]
    assert context["silences"] == [{"start": 15.0, "end": 16.0}]
    scene = context["analysis"]["scenes"][0]
    assert scene["start"] == 0.0 and scene["end"] == 20.0 and "shot_type" not in scene  # compact form
    assert "words" not in context["transcript_segments"][0]


async def test_plan_with_a_hard_error_falls_back() -> None:
    overlapping = make_plan([(0.0, 10.0), (5.0, 15.0)])
    plan, source, usage = await build_initial_plan(FakeGeminiClient(plan=overlapping), job=_job(), ctx=_ctx())
    assert source == PlanSource.FALLBACK and usage == UsageInfo(1000, 200)  # tokens were still spent
    assert validate_plan(plan, DURATION, get_preset("dynamic_reels"), {"upbeat_01"})[1] == []
    assert plan.human_summary_uz.startswith("Videodan ortiqcha pauzalarni olib tashladim")


async def test_ai_failure_falls_back_and_keeps_the_spent_usage() -> None:
    error = AIError("boom", usage=UsageInfo(700, 10))
    plan, source, usage = await build_initial_plan(FakeGeminiClient(plan_error=error), job=_job(), ctx=_ctx())
    assert source == PlanSource.FALLBACK and usage == UsageInfo(700, 10) and plan.clips


async def test_fallback_still_honours_job_settings() -> None:
    error = AIError("boom")
    job = _job(aspect="1:1", style_preset="ad_commercial", is_trial=True)
    plan, source, _ = await build_initial_plan(
        FakeGeminiClient(plan_error=error), job=job, ctx=_ctx(), watermark_text="@video_editor_uzbot"
    )
    assert source == PlanSource.FALLBACK
    assert (plan.target.aspect, plan.style_preset) == ("1:1", "ad_commercial")
    assert plan.watermark.model_dump() == {"enabled": True, "text": "@video_editor_uzbot"}
    assert plan.total_duration() <= 45.0 + 1e-6  # ad_commercial target maximum


async def test_no_speech_disables_captions_even_if_the_llm_enabled_them() -> None:
    plan, _, _ = await build_initial_plan(
        FakeGeminiClient(plan=make_plan([(0.0, 4.0)])), job=_job(), ctx=_ctx(with_speech=False)
    )
    assert plan.captions.enabled is False


async def test_unknown_music_track_from_the_llm_is_switched_off() -> None:
    llm_plan = make_plan([(0.0, 4.0)], music={"enabled": True, "track_id": "invented"})
    plan, source, _ = await build_initial_plan(FakeGeminiClient(plan=llm_plan), job=_job(), ctx=_ctx())
    assert source == PlanSource.AI_INITIAL and plan.music.enabled is False


async def test_llm_cannot_switch_on_a_watermark_for_paid_jobs() -> None:
    llm_plan = make_plan([(0.0, 4.0)], watermark={"enabled": True, "text": "SPAM"})
    plan, _, _ = await build_initial_plan(FakeGeminiClient(plan=llm_plan), job=_job(), ctx=_ctx())
    assert plan.watermark.enabled is False


# ---------- build_revised_plan ----------


async def test_revision_returns_the_new_plan_and_notes() -> None:
    current = make_plan([(0.0, 4.0), (20.0, 24.0)])
    revised = make_plan([(0.0, 4.0)])
    gemini = FakeGeminiClient(revision=(revised, ["2-qism olib tashlandi"], []))
    plan, changes, unsupported, source, usage = await build_revised_plan(
        gemini, job=_job(), current_plan=current, message="2-qismni o‘chir", ctx=_ctx()
    )
    assert source == PlanSource.AI_REVISION and usage == UsageInfo(500, 100)
    assert len(plan.clips) == 1 and changes == ["2-qism olib tashlandi"] and unsupported == []
    [call] = gemini.revise_calls
    assert call["message"] == "2-qismni o‘chir" and "analysis_compact" in call["context"]


async def test_revision_ai_error_keeps_the_current_plan() -> None:
    current = make_plan([(0.0, 4.0)])
    gemini = FakeGeminiClient(revise_error=AIError("down", usage=UsageInfo(5, 5)))
    plan, changes, unsupported, _, usage = await build_revised_plan(
        gemini, job=_job(), current_plan=current, message="x", ctx=_ctx()
    )
    assert plan == current and changes == []
    assert unsupported == [REVISION_FAILED_UZ] and usage == UsageInfo(5, 5)


async def test_revision_with_a_hard_error_keeps_the_current_plan() -> None:
    current = make_plan([(0.0, 4.0)])
    broken = make_plan([(0.0, 10.0), (5.0, 15.0)])
    gemini = FakeGeminiClient(revision=(broken, ["changed"], []))
    plan, changes, unsupported, _, _ = await build_revised_plan(
        gemini, job=_job(), current_plan=current, message="x", ctx=_ctx()
    )
    assert plan == current and changes == [] and unsupported == [REVISION_FAILED_UZ]


async def test_revision_cannot_change_the_format() -> None:
    revised = make_plan([(0.0, 4.0)], aspect="1:1")
    gemini = FakeGeminiClient(revision=(revised, [], []))
    plan, *_ = await build_revised_plan(
        gemini, job=_job(), current_plan=make_plan([(0.0, 4.0)]), message="1:1 qil", ctx=_ctx()
    )
    assert plan.target.aspect == "9:16"


# ---------- plan_text ----------


def test_compact_transcript_merges_to_at_most_max_segments_and_truncates() -> None:
    words = [(f"word{i}", i * 2.0, i * 2.0 + 0.5) for i in range(1000)]
    transcript = make_transcript(words, segment_gap=1.0)
    assert len(transcript.segments) == 1000
    compact = compact_transcript(transcript, max_segments=400, max_chars=200)
    assert len(compact) <= 400 and all(len(s["text"]) <= 200 for s in compact)
    assert compact[0]["start"] == 0.0 and compact[-1]["end"] == pytest.approx(1998.5, abs=0.01)


def test_compact_transcript_empty() -> None:
    assert compact_transcript(make_transcript([])) == []


def test_compact_analysis_adds_times_and_drops_heavy_fields() -> None:
    analysis = VideoAnalysis(scenes=[SceneAnalysis(scene_id=1, subjects=["car"], on_screen_text=["x"])])
    compact = compact_analysis(analysis, [Scene(1, 0.0, 4.25)])
    assert set(compact["scenes"][0]) == {
        "scene_id", "start", "end", "description", "highlight_score",
        "role_suggestion", "usable", "focus_x", "focus_y", "problems",
    }  # fmt: skip
    assert compact["scenes"][0]["end"] == 4.25
