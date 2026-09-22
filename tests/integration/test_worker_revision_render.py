import pytest

from app.bot import texts
from app.core.errors import AIError
from app.models.enums import JobStatus, PlanSource
from app.schemas.edit_plan import EditPlan
from app.services.media.probe import probe
from app.services.render.engine import render_plan as real_render_plan
from app.worker import tasks
from app.worker.tasks import run_render, run_revision
from tests.fakes.gemini import make_plan

pytestmark = pytest.mark.slow

AWAITING = JobStatus.AWAITING_PLAN_APPROVAL


async def _ready_job(harness, **kw):  # noqa: ANN001, ANN003, ANN202
    """A job waiting for approval with plan v1 and all analysis artifacts in Blob."""
    h = await harness.new_job(status=AWAITING, current_plan_version=1, **kw)
    await harness.add_plan(h.job_id)
    harness.seed_artifacts(h.job_id)
    return h


# ---------- revision ----------


async def test_free_revision_creates_plan_v2(harness) -> None:  # noqa: ANN001
    h = await _ready_job(harness)
    harness.gemini.revision = (
        make_plan([(0.0, 2.5)]),
        ["2-qism olib tashlandi"],
        ["Effekt qo‘shib bo‘lmaydi"],
    )
    await run_revision(harness.ctx, str(h.job_id), "2-qismni o‘chir")

    job = await harness.job(h.job_id)
    assert (job.status, job.revision_count, job.current_plan_version) == (AWAITING, 1, 2)
    v1, v2 = await harness.plans(h.job_id)
    assert (v2.version, v2.source, v2.user_feedback) == (2, PlanSource.AI_REVISION, "2-qismni o‘chir")
    assert (
        len(EditPlan.model_validate(v2.plan_json).clips)
        < len(EditPlan.model_validate(v1.plan_json).clips) + 5
    )
    [ready] = harness.notifier.of("plan_ready")
    assert ready[3] == ["2-qism olib tashlandi"] and ready[4] == ["Effekt qo‘shib bo‘lmaydi"]
    assert texts.PROGRESS_REVISING in harness.notifier.texts()
    assert await harness.ledger(h.user_id) == [("reserve", -1)]  # free: no charge
    assert (job.llm_input_tokens, job.llm_output_tokens) == (500, 100)
    assert not (harness.tmp_dir / f"{h.job_id}-rev").exists()


async def test_the_third_revision_is_paid(harness) -> None:  # noqa: ANN001
    h = await _ready_job(harness, balance=3, revision_count=2)
    harness.gemini.revision = (make_plan([(0.0, 2.5)]), ["qisqartirildi"], [])
    await run_revision(harness.ctx, str(h.job_id), "qisqartir")
    job = await harness.job(h.job_id)
    assert (job.revision_count, job.current_plan_version) == (3, 2)
    assert await harness.ledger(h.user_id) == [("reserve", -1), ("revision", -1)]
    assert (await harness.user(h.user_id)).balance_units == 1


async def test_exhausted_free_revisions_without_balance_do_not_fail_the_job(harness) -> None:  # noqa: ANN001
    h = await _ready_job(harness, balance=1, revision_count=2)  # the only unit is reserved: balance 0
    await run_revision(harness.ctx, str(h.job_id), "yana o‘zgartir")
    job = await harness.job(h.job_id)
    assert job.status == AWAITING and job.revision_count == 2 and job.current_plan_version == 1
    assert len(await harness.plans(h.job_id)) == 1  # no new version
    assert harness.notifier.texts() == [texts.PROGRESS_REVISING, texts.FREE_EDITS_EXHAUSTED]
    assert harness.gemini.revise_calls == []  # the LLM was not even called
    assert harness.notifier.of("failed") == []


async def test_llm_failure_keeps_the_plan_and_charges_nothing(harness) -> None:  # noqa: ANN001
    h = await _ready_job(harness, balance=3, revision_count=2)
    harness.gemini.revise_error = AIError("down", usage=None)
    await run_revision(harness.ctx, str(h.job_id), "qisqartir")
    job = await harness.job(h.job_id)
    assert (job.status, job.current_plan_version, job.revision_count) == (AWAITING, 1, 2)
    assert len(await harness.plans(h.job_id)) == 1
    assert await harness.ledger(h.user_id) == [("reserve", -1)]  # a failed paid revision costs nothing
    assert texts.REVISION_NOT_APPLIED in harness.notifier.texts()


async def test_missing_artifacts_do_not_fail_the_job(harness) -> None:  # noqa: ANN001
    h = await harness.new_job(status=AWAITING, current_plan_version=1)
    await harness.add_plan(h.job_id)  # but no artifacts were saved (expired)
    await run_revision(harness.ctx, str(h.job_id), "x")
    assert (await harness.job(h.job_id)).status == AWAITING
    assert texts.REVISION_NOT_APPLIED in harness.notifier.texts()


async def test_double_click_on_revise_is_ignored(harness) -> None:  # noqa: ANN001
    h = await _ready_job(harness)
    await harness.set_status(h.job_id, JobStatus.REVISING)  # first click is already being processed
    await run_revision(harness.ctx, str(h.job_id), "x")
    assert harness.notifier.calls == [] and harness.gemini.revise_calls == []


# ---------- render ----------


async def test_render_delivers_and_marks_done(harness) -> None:  # noqa: ANN001
    h = await _ready_job(harness)
    await run_render(harness.ctx, str(h.job_id))

    job = await harness.job(h.job_id)
    assert job.status == JobStatus.DONE and job.finished_at is not None and job.error_code is None
    assert job.output_blob_path == f"{h.job_id}/final.mp4" and job.render_seconds > 0
    final = harness.storage.blobs[("outputs", job.output_blob_path)]
    assert job.output_size_bytes == len(final)
    assert [c[0] for c in harness.notifier.calls] == ["progress", "delivered"]
    assert harness.notifier.texts() == [texts.PROGRESS_RENDERING]
    # rendered from the ORIGINAL upload, never from the proxy
    assert ("uploads", h.blob_path) in harness.storage.downloads
    assert ("artifacts", f"{h.job_id}/proxy.mp4") not in harness.storage.downloads
    assert not (harness.tmp_dir / str(h.job_id)).exists()

    out = harness.tmp_dir.parent / "out.mp4"
    out.write_bytes(final)
    info = await probe(str(out))
    assert (info.width, info.height) == (1080, 1920) and info.duration_sec == pytest.approx(4.6, abs=0.5)


async def test_render_failure_fails_the_job_and_refunds(harness, monkeypatch) -> None:  # noqa: ANN001
    async def boom(**_kw: object) -> None:
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(tasks, "render_plan", boom)
    h = await _ready_job(harness)
    await run_render(harness.ctx, str(h.job_id))
    job = await harness.job(h.job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "RENDER_FAILED")
    # ARCHITECTURE section 7: FAILED refunds everything reserved (the P09 prompt says otherwise; docs win)
    assert await harness.ledger(h.user_id) == [("reserve", -1), ("refund", 1)]
    [failed] = harness.notifier.of("failed")
    assert failed[2] == texts.FAILED_RENDER
    assert not (harness.tmp_dir / str(h.job_id)).exists()


async def test_missing_transcript_artifact_is_a_render_failure(harness) -> None:  # noqa: ANN001
    h = await harness.new_job(status=AWAITING, current_plan_version=1)
    await harness.add_plan(h.job_id)
    await run_render(harness.ctx, str(h.job_id))
    assert (await harness.job(h.job_id)).error_code == "RENDER_FAILED"


async def test_a_failing_status_message_update_does_not_fail_a_delivered_job(harness) -> None:  # noqa: ANN001
    harness.notifier.raise_on = {"delivered"}  # closing the status message is best-effort
    h = await _ready_job(harness)
    await run_render(harness.ctx, str(h.job_id))
    job = await harness.job(h.job_id)
    assert job.status == JobStatus.DONE and job.error_code is None
    assert await harness.ledger(h.user_id) == [("reserve", -1)]  # the user got the video: nothing is refunded


async def test_an_existing_final_video_is_reused(harness, monkeypatch) -> None:  # noqa: ANN001
    async def boom(**_kw: object) -> None:
        raise AssertionError("must not render again")

    monkeypatch.setattr(tasks, "render_plan", boom)
    h = await _ready_job(harness)
    harness.storage.put("outputs", f"{h.job_id}/final.mp4", b"already-rendered")
    await run_render(harness.ctx, str(h.job_id))
    job = await harness.job(h.job_id)
    assert (job.status, job.output_size_bytes) == (JobStatus.DONE, len(b"already-rendered"))
    assert [c[0] for c in harness.notifier.calls] == ["progress", "delivered"]


async def test_render_only_starts_from_awaiting_approval(harness) -> None:  # noqa: ANN001
    h = await harness.new_job(status=JobStatus.QUEUED)
    await run_render(harness.ctx, str(h.job_id))
    assert (await harness.job(h.job_id)).status == JobStatus.QUEUED and harness.notifier.calls == []


async def test_long_videos_render_in_light_mode(harness, monkeypatch) -> None:  # noqa: ANN001
    seen: dict = {}

    async def spy(**kw):  # noqa: ANN003, ANN202
        seen.update(
            max_short_side=kw["max_short_side"], preset=kw["plan"].export.preset, crf=kw["plan"].export.crf
        )
        return await real_render_plan(**kw)

    monkeypatch.setattr(tasks, "render_plan", spy)
    harness.settings.long_video_threshold_sec = 1  # the 6 s test video counts as long
    h = await _ready_job(harness)
    await run_render(harness.ctx, str(h.job_id))
    assert seen == {"max_short_side": 720, "preset": "ultrafast", "crf": 21}
    assert (await harness.job(h.job_id)).status == JobStatus.DONE


# ---------- referral reward ----------


async def test_inviter_gets_3_units_after_the_first_done_job_only(harness) -> None:  # noqa: ANN001
    inviter = await harness.new_job(balance=0, reserve=0)
    first = await _ready_job(harness, referred_by=inviter.user_id)
    await run_render(harness.ctx, str(first.job_id))
    assert (await harness.user(inviter.user_id)).balance_units == 3
    assert await harness.ledger(inviter.user_id) == [("referral", 3)]

    second = await harness.new_job(status=AWAITING, current_plan_version=1, user_id=first.user_id)
    await harness.add_plan(second.job_id)
    harness.seed_artifacts(second.job_id)
    await run_render(harness.ctx, str(second.job_id))
    assert (await harness.job(second.job_id)).status == JobStatus.DONE
    assert (await harness.user(inviter.user_id)).balance_units == 3  # not rewarded twice


async def test_no_reward_without_a_referrer(harness) -> None:  # noqa: ANN001
    h = await _ready_job(harness)
    await run_render(harness.ctx, str(h.job_id))
    assert await harness.ledger(h.user_id) == [("reserve", -1)]
