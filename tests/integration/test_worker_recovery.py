import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import JobStatus
from app.worker.main import WorkerSettings
from app.worker.recovery import requeue_stuck_jobs
from app.worker.tasks import cleanup_expired, requeue_stuck


def _ago(**kw: float) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


async def _recover(harness):  # noqa: ANN001, ANN202
    async with harness.session_factory() as session:
        return await requeue_stuck_jobs(session, harness.deps.enqueue, harness.notifier)


# ---------- stuck jobs ----------


async def test_stuck_job_is_requeued_once_then_failed_and_refunded(harness) -> None:  # noqa: ANN001
    h = await harness.new_job(status=JobStatus.ANALYZING)
    await harness.set_updated_at(h.job_id, _ago(minutes=40))

    report = await _recover(harness)
    assert (report.requeued, report.failed) == (1, 0)
    job = await harness.job(h.job_id)
    assert (job.status, job.error_code) == (JobStatus.QUEUED, "STUCK_RETRY")
    assert harness.enqueued == [("run_analysis", str(h.job_id))]

    # stuck again (the retry did not finish either)
    await harness.set_status(h.job_id, JobStatus.ANALYZING)
    await harness.set_updated_at(h.job_id, _ago(minutes=40))
    report = await _recover(harness)
    assert (report.requeued, report.failed) == (0, 1)
    job = await harness.job(h.job_id)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "STUCK_RETRY")
    assert await harness.ledger(h.user_id) == [("reserve", -1), ("refund", 1)]
    assert len(harness.enqueued) == 1  # not enqueued a second time
    [failed] = harness.notifier.of("failed")
    assert failed[1] == h.job_id


@pytest.mark.parametrize(
    ("stuck", "back_to", "task"),
    [
        (JobStatus.PREPROCESSING, JobStatus.QUEUED, "run_analysis"),
        (JobStatus.ANALYZING, JobStatus.QUEUED, "run_analysis"),
        (JobStatus.PLANNING, JobStatus.QUEUED, "run_analysis"),
        (JobStatus.RENDERING, JobStatus.AWAITING_PLAN_APPROVAL, "run_render"),
        (JobStatus.DELIVERING, JobStatus.AWAITING_PLAN_APPROVAL, "run_render"),
        (JobStatus.REVISING, JobStatus.AWAITING_PLAN_APPROVAL, None),
    ],
)
async def test_each_stuck_state_steps_back_to_a_rerunnable_state(harness, stuck, back_to, task) -> None:  # noqa: ANN001
    h = await harness.new_job(status=stuck)
    await harness.set_updated_at(h.job_id, _ago(minutes=31))
    await _recover(harness)
    assert (await harness.job(h.job_id)).status == back_to
    assert harness.enqueued == ([(task, str(h.job_id))] if task else [])


async def test_recent_and_terminal_and_idle_jobs_are_left_alone(harness) -> None:  # noqa: ANN001
    fresh = await harness.new_job(status=JobStatus.ANALYZING)
    await harness.set_updated_at(fresh.job_id, _ago(minutes=5))
    done = await harness.new_job(status=JobStatus.DONE)
    queued = await harness.new_job(status=JobStatus.QUEUED)
    waiting = await harness.new_job(status=JobStatus.AWAITING_PLAN_APPROVAL)
    for h in (done, queued, waiting):
        await harness.set_updated_at(h.job_id, _ago(hours=5))
    report = await _recover(harness)
    assert (report.requeued, report.failed) == (0, 0) and harness.enqueued == []
    assert (await harness.job(fresh.job_id)).status == JobStatus.ANALYZING
    assert (await harness.job(waiting.job_id)).status == JobStatus.AWAITING_PLAN_APPROVAL


async def test_a_trial_job_stuck_twice_gives_the_trial_back(harness) -> None:  # noqa: ANN001
    h = await harness.new_job(status=JobStatus.RENDERING, is_trial=True, balance=0)
    await harness.set_updated_at(h.job_id, _ago(minutes=40))
    await harness.set_status(h.job_id, JobStatus.RENDERING, error_code="STUCK_RETRY")
    await harness.set_updated_at(h.job_id, _ago(minutes=40))
    await _recover(harness)
    assert (await harness.job(h.job_id)).status == JobStatus.FAILED
    assert (await harness.user(h.user_id)).trial_used is False


async def test_the_cron_wrapper_uses_the_deps(harness) -> None:  # noqa: ANN001
    h = await harness.new_job(status=JobStatus.PLANNING)
    await harness.set_updated_at(h.job_id, _ago(minutes=45))
    await requeue_stuck(harness.ctx)
    assert harness.enqueued == [("run_analysis", str(h.job_id))]


# ---------- cleanup ----------


async def test_cleanup_deletes_old_blobs_tmp_dirs_and_expires_stale_plans(harness) -> None:  # noqa: ANN001
    old, fresh = _ago(hours=60), _ago(hours=1)
    for container in ("uploads", "artifacts", "outputs"):
        harness.storage.put(container, "old/x.bin", b"1", modified=old)
        harness.storage.put(container, "new/x.bin", b"1", modified=fresh)

    tmp = harness.tmp_dir
    (tmp / "crashed-job").mkdir(parents=True)
    (tmp / "running-job").mkdir()
    long_ago = time.time() - 7 * 3600
    os.utime(tmp / "crashed-job", (long_ago, long_ago))

    stale = await harness.new_job(status=JobStatus.AWAITING_PLAN_APPROVAL)
    recent = await harness.new_job(status=JobStatus.AWAITING_PLAN_APPROVAL)
    await harness.set_updated_at(stale.job_id, _ago(hours=25))
    await harness.set_updated_at(recent.job_id, _ago(hours=23))

    result = await cleanup_expired(harness.ctx)

    assert result == {"blobs": 3, "tmp_dirs": 1, "expired_plans": 1}
    for container in ("uploads", "artifacts", "outputs"):
        assert (container, "old/x.bin") not in harness.storage.blobs
        assert (container, "new/x.bin") in harness.storage.blobs
    assert not (tmp / "crashed-job").exists() and (tmp / "running-job").exists()
    assert (await harness.job(stale.job_id)).status == JobStatus.EXPIRED
    assert (await harness.job(recent.job_id)).status == JobStatus.AWAITING_PLAN_APPROVAL
    assert await harness.ledger(stale.user_id) == [("reserve", -1)]  # EXPIRED: no refund


async def test_cleanup_with_nothing_to_do(harness) -> None:  # noqa: ANN001
    assert await cleanup_expired(harness.ctx) == {"blobs": 0, "tmp_dirs": 0, "expired_plans": 0}


# ---------- arq configuration ----------


def test_worker_settings() -> None:
    names = [f.__name__ for f in WorkerSettings.functions]
    assert names == ["run_analysis", "run_revision", "run_render", "cleanup_expired"]
    assert WorkerSettings.max_tries == 1  # our own recovery is the only retry path
    assert WorkerSettings.job_timeout == 14400
    cron_names = {job.name for job in WorkerSettings.cron_jobs}
    assert {"cron:cleanup_expired", "cron:requeue_stuck"} == cron_names
    assert WorkerSettings.on_startup is not None and WorkerSettings.on_shutdown is not None
