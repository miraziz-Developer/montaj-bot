"""Plan orchestration: LLM plan -> snap -> rhythm -> validate, with the deterministic fallback."""

import logging
from typing import Any

from app.core.errors import AIError
from app.models.enums import PlanSource
from app.models.job import Job
from app.schemas.edit_plan import Clip, EditPlan
from app.services.ai.fallback_planner import build_fallback_plan
from app.services.ai.llm import LLMClient, UsageInfo
from app.services.ai.plan_text import PlanContext
from app.services.ai.plan_validator import (
    DEFAULT_WATERMARK_TEXT,
    apply_rhythm,
    force_job_settings,
    renumber,
    validate_plan,
)
from app.services.ai.presets import PresetRules, get_preset
from app.services.ai.snap import snap_cuts

logger = logging.getLogger(__name__)

REVISION_FAILED_UZ = "Kechirasiz, bu o‘zgarishni amalga oshira olmadim."
DEFAULT_ASPECT = "9:16"
DEFAULT_STYLE = "dynamic_reels"


def job_settings(job: Job) -> dict[str, Any]:
    return {
        "aspect": job.aspect or DEFAULT_ASPECT,
        "style_preset": job.style_preset or DEFAULT_STYLE,
        "brief": job.brief or "",
    }


def _finalize(
    plan: EditPlan, ctx: PlanContext, job: Job, preset: PresetRules, watermark_text: str
) -> tuple[EditPlan, list[str]]:
    """force_job_settings -> snap_cuts -> apply_rhythm -> validate_plan (docs section 4, order matters)."""
    settings = job_settings(job)
    has_words = bool(ctx.transcript.all_words())

    def force(p: EditPlan) -> EditPlan:
        return force_job_settings(
            p,
            aspect=settings["aspect"],
            style_preset=settings["style_preset"],
            is_trial=job.is_trial,
            has_words=has_words,
            watermark_text=watermark_text,
        )

    plan = force(plan)
    plan = snap_cuts(plan, ctx.transcript, ctx.silences)
    plan = apply_rhythm(plan, ctx.transcript, preset)
    plan, errors = validate_plan(plan, ctx.source.duration_sec, preset, ctx.music_ids)
    return force(plan), errors


def _whole_video_plan(ctx: PlanContext, job: Job, preset: PresetRules, watermark_text: str) -> EditPlan:
    """Last resort if even the fallback plan fails validation: the whole source, untouched."""
    settings = job_settings(job)
    plan = EditPlan(
        title=f"{preset.label} montaj",
        style_preset=settings["style_preset"],
        target={"aspect": settings["aspect"]},  # type: ignore[arg-type]
        clips=[Clip(id="c1", src_in=0.0, src_out=ctx.source.duration_sec)],
    )
    return force_job_settings(
        renumber(plan),
        aspect=settings["aspect"],
        style_preset=settings["style_preset"],
        is_trial=job.is_trial,
        has_words=bool(ctx.transcript.all_words()),
        watermark_text=watermark_text,
    )


def _fallback(ctx: PlanContext, job: Job, preset: PresetRules, watermark_text: str) -> EditPlan:
    settings = job_settings(job)
    plan = build_fallback_plan(
        source_duration=ctx.source.duration_sec,
        transcript=ctx.transcript,
        silences=ctx.silences,
        preset=settings["style_preset"],
        preset_rules=preset,
        music_tracks=ctx.music_tracks,
        aspect=settings["aspect"],
        is_trial=job.is_trial,
    )
    plan, errors = validate_plan(plan, ctx.source.duration_sec, preset, ctx.music_ids)
    if errors:
        logger.error("fallback plan invalid job_id=%s errors=%s", job.id, errors)
        return _whole_video_plan(ctx, job, preset, watermark_text)
    return force_job_settings(
        plan,
        aspect=settings["aspect"],
        style_preset=settings["style_preset"],
        is_trial=job.is_trial,
        has_words=bool(ctx.transcript.all_words()),
        watermark_text=watermark_text,
    )


async def build_initial_plan(
    gemini: LLMClient,
    *,
    job: Job,
    ctx: PlanContext,
    watermark_text: str = DEFAULT_WATERMARK_TEXT,
) -> tuple[EditPlan, PlanSource, UsageInfo | None]:
    """AI plan if it survives post-processing; otherwise the deterministic fallback (never fails)."""
    settings = job_settings(job)
    preset = get_preset(settings["style_preset"])
    usage: UsageInfo | None = None
    try:
        plan, usage = await gemini.plan(context=ctx.planner_input(settings, preset))
    except AIError as exc:
        usage = exc.usage
        logger.warning("planner LLM failed job_id=%s: %s", job.id, exc.detail)
        return _fallback(ctx, job, preset, watermark_text), PlanSource.FALLBACK, usage

    plan, errors = _finalize(plan, ctx, job, preset, watermark_text)
    if errors:
        logger.warning("AI plan rejected job_id=%s errors=%s", job.id, errors)
        return _fallback(ctx, job, preset, watermark_text), PlanSource.FALLBACK, usage
    return plan, PlanSource.AI_INITIAL, usage


async def build_revised_plan(
    gemini: LLMClient,
    *,
    job: Job,
    current_plan: EditPlan,
    message: str,
    ctx: PlanContext,
    watermark_text: str = DEFAULT_WATERMARK_TEXT,
) -> tuple[EditPlan, list[str], list[str], PlanSource, UsageInfo | None]:
    """(plan, changes_uz, unsupported_uz, source, usage). On any failure the CURRENT plan is returned
    unchanged with an apology in `unsupported_uz` (callers detect "no change" by `plan == current_plan`)."""
    settings = job_settings(job)
    preset = get_preset(settings["style_preset"])
    context = ctx.revision_context(settings, preset)
    unchanged = (current_plan, [], [REVISION_FAILED_UZ], PlanSource.AI_REVISION)
    try:
        plan, changes, unsupported, usage = await gemini.revise(
            current_plan=current_plan, message=message, context=context
        )
    except AIError as exc:
        logger.warning("revision LLM failed job_id=%s: %s", job.id, exc.detail)
        return (*unchanged, exc.usage)

    plan, errors = _finalize(plan, ctx, job, preset, watermark_text)
    if errors:
        logger.warning("revised plan rejected job_id=%s errors=%s", job.id, errors)
        return (*unchanged, usage)
    return plan, changes, unsupported, PlanSource.AI_REVISION, usage
