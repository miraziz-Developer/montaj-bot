import logging
import uuid
from decimal import Decimal

import pytest

from app.bot import texts
from app.core.errors import AIError, InvalidMedia
from app.models.enums import JobStatus, PlanSource
from app.schemas.edit_plan import EditPlan
from app.services import jobs
from app.services.ai.llm import UsageInfo
from app.services.media.ffmpeg import FFmpegError
from app.services.stt.base import STTError
from app.worker.costs import estimate_cost_usd
from app.worker.failures import ErrorCode, Stage, classify_error, friendly_uz_message_for
from app.worker.tasks import run_analysis
from tests.fakes.gemini import default_analysis

pytestmark = pytest.mark.slow


# ---------- happy path ----------


async def test_run_analysis_ends_in_awaiting_approval_with_plan_v1(harness) -> None:  # noqa: ANN001
    h = await harness.new_job()
    await run_analysis(harness.ctx, str(h.job_id))

    job = await harness.job(h.job_id)
    assert job.status == JobStatus.AWAITING_PLAN_APPROVAL
    assert job.current_plan_version == 1 and job.error_code is None
    [row] = await harness.plans(h.job_id)
    assert (row.version, row.source) == (1, PlanSource.AI_INITIAL)
    plan = EditPlan.model_validate(row.plan_json)
    assert plan.target.aspect == "9:16" and plan.clips

    [ready] = harness.notifier.of("plan_ready")
    assert ready[1] == h.job_id and ready[2].clips
    assert harness.notifier.of("failed") == []
    assert harness.notifier.texts() == [
        texts.PROGRESS_PREPARING,
        texts.PROGRESS_ANALYZING,
        texts.PROGRESS_PLANNING,
    ]

    for name in (
        "proxy.mp4",
        "transcript.json",
        "scenes.json",
        "silences.json",
        "analysis.json",
        "plan_v1.json",
    ):
        assert ("artifacts", f"{h.job_id}/{name}") in harness.storage.blobs, name
    assert not (harness.tmp_dir / str(h.job_id)).exists()  # workdir removed


async def test_costs_are_recorded(harness) -> None:  # noqa: ANN001
    h = await harness.new_job()
    await run_analysis(harness.ctx, str(h.job_id))
    job = await harness.job(h.job_id)
    assert (job.llm_input_tokens, job.llm_output_tokens) == (
        100 + 1000,
        20 + 200,
    )  # 1 analysis window + the plan
    assert job.stt_seconds == pytest.approx(6.0, abs=0.2)
    expected = estimate_cost_usd(
        llm_input_tokens=1100,
        llm_output_tokens=220,
        stt_seconds=job.stt_seconds,
        render_seconds=0,
        settings=harness.settings,
    )
    assert job.est_cost_usd == expected and job.est_cost_usd > Decimal("0")


async def test_a_failing_plan_falls_back_but_the_job_still_completes(harness) -> None:  # noqa: ANN001
    harness.gemini.plan_error = AIError("planner down", usage=UsageInfo(5, 5))
    h = await harness.new_job()
    await run_analysis(harness.ctx, str(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.AWAITING_PLAN_APPROVAL
    assert (await harness.plans(h.job_id))[0].source == PlanSource.FALLBACK


async def test_cached_analysis_artifact_is_reused(harness) -> None:  # noqa: ANN001
    h = await harness.new_job()
    scenes = [{"scene_id": 1}, {"scene_id": 2}]
    harness.storage.put(
        "artifacts", f"{h.job_id}/analysis.json", default_analysis(scenes).model_dump_json().encode()
    )
    await run_analysis(harness.ctx, str(h.job_id))
    assert harness.gemini.analyze_calls == []  # no second LLM bill
    assert (await harness.job(h.job_id)).status == JobStatus.AWAITING_PLAN_APPROVAL


async def test_a_job_that_is_not_queued_is_left_alone(harness) -> None:  # noqa: ANN001
    h = await harness.new_job(status=JobStatus.AWAITING_CONFIRM)
    await run_analysis(harness.ctx, str(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.AWAITING_CONFIRM
    assert harness.notifier.calls == [] and harness.stt.calls == []


async def test_notification_failures_do_not_fail_the_job(harness) -> None:  # noqa: ANN001
    harness.notifier.raise_on = {"progress", "plan_ready"}
    h = await harness.new_job()
    await run_analysis(harness.ctx, str(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.AWAITING_PLAN_APPROVAL


# ---------- failure handling ----------


async def test_stt_failure_fails_the_job_and_refunds(harness) -> None:  # noqa: ANN001
    async def boom(*_a: object, **_k: object) -> None:
        raise STTError("Groq returned HTTP 500")

    harness.stt.transcribe = boom  # type: ignore[method-assign]
    h = await harness.new_job(balance=5, reserve=1)

    await run_analysis(harness.ctx, str(h.job_id))

    job = await harness.job(h.job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "STT_FAILED") and job.finished_at is not None
    assert await harness.ledger(h.user_id) == [("reserve", -1), ("refund", 1)]
    assert (await harness.user(h.user_id)).balance_units == 5  # everything came back
    [failed] = harness.notifier.of("failed")
    assert failed[2] == texts.FAILED_STT and failed[3] == "STT_FAILED"
    assert harness.notifier.of("plan_ready") == []
    assert not (harness.tmp_dir / str(h.job_id)).exists()


async def test_analysis_llm_failure_is_analysis_failed(harness) -> None:  # noqa: ANN001
    async def boom(**_kw: object) -> None:
        raise AIError("Gemini down")

    harness.gemini.analyze_video = boom  # type: ignore[method-assign]
    h = await harness.new_job()
    await run_analysis(harness.ctx, str(h.job_id))
    job = await harness.job(h.job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "ANALYSIS_FAILED")
    assert await harness.ledger(h.user_id) == [("reserve", -1), ("refund", 1)]


async def test_trial_job_failure_gives_the_trial_back(harness) -> None:  # noqa: ANN001
    async def boom(*_a: object, **_k: object) -> None:
        raise STTError("down")

    harness.stt.transcribe = boom  # type: ignore[method-assign]
    h = await harness.new_job(is_trial=True, balance=0)
    assert (await harness.user(h.user_id)).trial_used is True
    await run_analysis(harness.ctx, str(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.FAILED
    assert (await harness.user(h.user_id)).trial_used is False
    assert await harness.ledger(h.user_id) == []  # trial jobs never touch the ledger


async def test_missing_source_is_a_download_failure(harness) -> None:  # noqa: ANN001
    h = await harness.new_job()
    harness.storage.blobs.clear()  # the upload vanished from Blob
    await run_analysis(harness.ctx, str(h.job_id))
    job = await harness.job(h.job_id)
    assert job.status == JobStatus.FAILED and job.error_code in {"INTERNAL_ERROR", "DOWNLOAD_FAILED"}
    assert (await harness.user(h.user_id)).balance_units == 5


async def test_a_failure_after_someone_else_moved_the_job_changes_nothing(harness) -> None:  # noqa: ANN001
    async def boom(*_a: object, **_k: object) -> None:
        raise STTError("down")

    harness.stt.transcribe = boom  # type: ignore[method-assign]
    h = await harness.new_job()
    original = harness.deps.notifier.progress

    async def cancel_meanwhile(job, text) -> None:  # noqa: ANN001
        await harness.set_status(h.job_id, JobStatus.CANCELED)  # the user canceled while we were working
        await original(job, text)

    harness.notifier.progress = cancel_meanwhile  # type: ignore[method-assign]
    await run_analysis(harness.ctx, str(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.CANCELED  # terminal states are never overwritten
    assert await harness.ledger(h.user_id) == [("reserve", -1)]  # and nothing is refunded twice or wrongly
    assert harness.notifier.of("failed") == []


# ---------- transition ----------


async def test_transition_returns_none_and_changes_nothing_for_a_stale_state(harness, caplog) -> None:  # noqa: ANN001
    h = await harness.new_job(status=JobStatus.ANALYZING)
    async with harness.session_factory() as session:
        with caplog.at_level(logging.WARNING):
            stale = await jobs.transition(session, h.job_id, [JobStatus.QUEUED], JobStatus.PREPROCESSING)
        assert stale is None and "transition skipped" in caplog.text
        moved = await jobs.transition(
            session, h.job_id, [JobStatus.ANALYZING], JobStatus.PLANNING, error_code="X"
        )
        await session.commit()
    assert moved is not None and (moved.status, moved.error_code) == (JobStatus.PLANNING, "X")
    assert (await harness.job(h.job_id)).status == JobStatus.PLANNING


async def test_transition_for_an_unknown_job_is_none(harness) -> None:  # noqa: ANN001
    async with harness.session_factory() as session:
        assert await jobs.transition(session, uuid.uuid4(), [JobStatus.QUEUED], JobStatus.FAILED) is None


# ---------- error classification ----------


@pytest.mark.parametrize(
    ("exc", "stage", "code"),
    [
        (STTError("x"), Stage.PREPARE, ErrorCode.STT_FAILED),
        (InvalidMedia(), Stage.PREPARE, ErrorCode.PROBE_FAILED),
        (FFmpegError("x"), Stage.PREPARE, ErrorCode.PROBE_FAILED),
        (OSError("disk"), Stage.PREPARE, ErrorCode.DOWNLOAD_FAILED),
        (RuntimeError("?"), Stage.PREPARE, ErrorCode.INTERNAL_ERROR),
        (AIError("x"), Stage.ANALYZE, ErrorCode.ANALYSIS_FAILED),
        (AIError("x"), Stage.PLAN, ErrorCode.PLANNING_FAILED),
        (RuntimeError("?"), Stage.ANALYZE, ErrorCode.ANALYSIS_FAILED),
        (RuntimeError("?"), Stage.PLAN, ErrorCode.PLANNING_FAILED),
        (FFmpegError("x"), Stage.RENDER, ErrorCode.RENDER_FAILED),
        (RuntimeError("?"), Stage.DELIVER, ErrorCode.DELIVERY_FAILED),
    ],
)
def test_classify_error(exc: BaseException, stage: Stage, code: ErrorCode) -> None:
    assert classify_error(exc, stage) == code


def test_every_error_code_has_an_uzbek_message() -> None:
    for code in ErrorCode:
        assert friendly_uz_message_for(code).endswith(".")


def test_estimate_cost_formula() -> None:
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        price_gemini_in_per_m_usd=0.30,
        price_gemini_out_per_m_usd=2.50,
        price_stt_per_hour_usd=0.04,
        price_vm_per_hour_usd=0.10,
    )
    cost = estimate_cost_usd(
        llm_input_tokens=1_000_000,
        llm_output_tokens=200_000,
        stt_seconds=1800,
        render_seconds=7200,
        settings=settings,
    )
    assert cost == Decimal("0.3") + Decimal("0.5") + Decimal("0.02") + Decimal("0.2")
