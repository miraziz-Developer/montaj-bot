import uuid
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.main import app
from app.models import Job, UnitLedger, User
from app.models.enums import JobStatus, LedgerReason
from tests.factories import create_job
from tests.integration.conftest import auth_headers

MakeUser = Callable[..., Awaitable[User]]
CONFIRM = {"aspect": "9:16", "style_preset": "dynamic_reels", "brief": "  Narxni boshida ko‘rsat  "}


async def _job(session: AsyncSession, user: User, **fields) -> Job:
    fields.setdefault("status", JobStatus.AWAITING_CONFIRM)
    fields.setdefault("units_cost", 2)
    job = await create_job(session, user.id, **fields)
    await session.commit()
    return job


async def _confirm(client: httpx.AsyncClient, tg_id: int, job: Job, body: dict | None = None):
    return await client.post(f"/api/jobs/{job.id}/confirm", json=body or CONFIRM, headers=auth_headers(tg_id))


async def _ledger(session: AsyncSession, user: User) -> list[tuple[LedgerReason, int]]:
    """Ledger rows as (reason, delta), ordered by delta so the result never depends on clock ordering."""
    rows = await session.execute(
        select(UnitLedger.reason, UnitLedger.delta).where(UnitLedger.user_id == user.id)
    )
    return sorted(((reason, delta) for reason, delta in rows.all()), key=lambda row: row[1])


# ---------- confirm ----------


async def test_confirm_reserves_units_and_enqueues(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession, enqueued: list
) -> None:
    user = await make_user(100, balance=5)
    job = await _job(session, user)
    response = await _confirm(client, 100, job)
    assert response.status_code == 200, response.text
    assert response.json() == {"job_id": str(job.id), "status": "QUEUED", "balance_units": 3}

    await session.refresh(job)
    assert (job.status, job.aspect, job.style_preset, job.brief) == (
        JobStatus.QUEUED,
        "9:16",
        "dynamic_reels",
        "Narxni boshida ko‘rsat",
    )
    assert await _ledger(session, user) == [(LedgerReason.RESERVE, -2)]
    assert enqueued == [("run_analysis", str(job.id))]
    assert job.chat_id == 100  # the private chat of the user: where the bot posts progress


async def test_confirm_with_zero_balance_is_402(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession, enqueued: list
) -> None:
    user = await make_user(100, balance=0)
    job = await _job(session, user)
    response = await _confirm(client, 100, job)
    assert (response.status_code, response.json()["error"]["code"]) == (402, "INSUFFICIENT_UNITS")
    await session.refresh(job)
    assert job.status == JobStatus.AWAITING_CONFIRM  # rolled back with the failed reservation
    assert await _ledger(session, user) == [] and enqueued == []


async def test_double_confirm_is_409_and_reserves_once(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession, enqueued: list
) -> None:
    user = await make_user(100, balance=5)
    job = await _job(session, user)
    assert (await _confirm(client, 100, job)).status_code == 200
    second = await _confirm(client, 100, job)
    assert (second.status_code, second.json()["error"]["code"]) == (409, "INVALID_STATE")
    assert await _ledger(session, user) == [(LedgerReason.RESERVE, -2)]
    assert len(enqueued) == 1


async def test_confirm_validates_body(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100)
    job = await _job(session, user)
    bad_bodies = [
        {**CONFIRM, "aspect": "4:3"},
        {**CONFIRM, "style_preset": "nope"},
        {**CONFIRM, "brief": "x" * 501},
        {"aspect": "9:16"},
    ]
    for body in bad_bodies:
        response = await _confirm(client, 100, job, body)
        assert (response.status_code, response.json()["error"]["code"]) == (422, "VALIDATION_ERROR"), body


async def test_confirm_accepts_500_char_brief_and_empty_brief(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100, balance=10)
    job = await _job(session, user)
    assert (await _confirm(client, 100, job, {**CONFIRM, "brief": "x" * 500})).status_code == 200
    job2 = await _job(session, user)
    assert (await _confirm(client, 100, job2, {**CONFIRM, "brief": "   "})).status_code == 200
    await session.refresh(job2)
    assert job2.brief is None


async def test_confirm_other_users_job_is_404(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    owner = await make_user(100)
    await make_user(200)
    job = await _job(session, owner)
    response = await _confirm(client, 200, job)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "NOT_FOUND")


async def test_confirm_trial_job_uses_trial_not_units(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100, balance=0)
    job = await _job(session, user, is_trial=True)
    response = await _confirm(client, 100, job)
    assert response.status_code == 200 and response.json()["balance_units"] == 0
    user = await session.get(User, user.id, populate_existing=True)
    assert user.trial_used is True
    assert await _ledger(session, user) == []


async def test_confirm_trial_job_after_trial_used_is_409(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100, balance=0, trial_used=True)
    job = await _job(session, user, is_trial=True)
    response = await _confirm(client, 100, job)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "TRIAL_UNAVAILABLE")
    await session.refresh(job)
    assert job.status == JobStatus.AWAITING_CONFIRM


async def test_enqueue_failure_does_not_fail_confirm(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    async def broken(*_args) -> None:
        raise RuntimeError("redis down")

    app.dependency_overrides[deps.get_enqueue] = lambda: broken
    user = await make_user(100, balance=5)
    job = await _job(session, user)
    response = await _confirm(client, 100, job)
    assert response.status_code == 200
    await session.refresh(job)
    assert job.status == JobStatus.QUEUED


# ---------- cancel ----------


async def test_cancel_queued_refunds(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100, balance=5)
    job = await _job(session, user)
    await _confirm(client, 100, job)
    response = await client.post(f"/api/jobs/{job.id}/cancel", headers=auth_headers(100))
    assert response.status_code == 200
    assert response.json() == {"job_id": str(job.id), "status": "CANCELED", "balance_units": 5}
    await session.refresh(job)
    assert job.status == JobStatus.CANCELED
    assert await _ledger(session, user) == [(LedgerReason.RESERVE, -2), (LedgerReason.REFUND, 2)]


async def test_cancel_queued_trial_releases_trial(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100, balance=0)
    job = await _job(session, user, is_trial=True)
    await _confirm(client, 100, job)
    assert (await client.post(f"/api/jobs/{job.id}/cancel", headers=auth_headers(100))).status_code == 200
    user = await session.get(User, user.id, populate_existing=True)
    assert user.trial_used is False


async def test_cancel_awaiting_confirm_needs_no_refund(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100, balance=5)
    job = await _job(session, user)
    response = await client.post(f"/api/jobs/{job.id}/cancel", headers=auth_headers(100))
    assert response.status_code == 200 and response.json()["balance_units"] == 5
    assert await _ledger(session, user) == []


async def test_cancel_after_analysis_no_refund(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100, balance=5)
    job = await _job(session, user)
    await _confirm(client, 100, job)
    async with session.bind.connect() as conn:  # move the job forward like the worker would
        from sqlalchemy import update

        await conn.execute(
            update(Job).where(Job.id == job.id).values(status=JobStatus.AWAITING_PLAN_APPROVAL)
        )
        await conn.commit()
    response = await client.post(f"/api/jobs/{job.id}/cancel", headers=auth_headers(100))
    assert response.status_code == 200 and response.json()["balance_units"] == 3
    assert await _ledger(session, user) == [(LedgerReason.RESERVE, -2)]


async def test_cancel_not_allowed_states_and_double_cancel(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100)
    rendering = await _job(session, user, status=JobStatus.RENDERING)
    response = await client.post(f"/api/jobs/{rendering.id}/cancel", headers=auth_headers(100))
    assert (response.status_code, response.json()["error"]["code"]) == (409, "INVALID_STATE")

    waiting = await _job(session, user)
    assert (await client.post(f"/api/jobs/{waiting.id}/cancel", headers=auth_headers(100))).status_code == 200
    assert (await client.post(f"/api/jobs/{waiting.id}/cancel", headers=auth_headers(100))).status_code == 409


async def test_cancel_other_users_job_is_404(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    owner = await make_user(100)
    await make_user(200)
    job = await _job(session, owner)
    response = await client.post(f"/api/jobs/{job.id}/cancel", headers=auth_headers(200))
    assert response.status_code == 404


# ---------- read ----------


async def test_get_job_public_view_hides_internal_fields(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100)
    job = await _job(
        session,
        user,
        status=JobStatus.FAILED,
        error_code="RENDER_FAILED",
        error_message="ffmpeg exploded https://blob/x?sig=SECRET",
        output_blob_path="outputs/x/final.mp4",
    )
    response = await client.get(f"/api/jobs/{job.id}", headers=auth_headers(100))
    body = response.json()
    assert response.status_code == 200
    assert body["id"] == str(job.id) and body["status"] == "FAILED" and body["error_code"] == "RENDER_FAILED"
    assert "error_message" not in body and "output_blob_path" not in body and "upload_id" not in body
    assert "SECRET" not in response.text


async def test_get_job_other_user_or_unknown_is_404(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    owner = await make_user(100)
    await make_user(200)
    job = await _job(session, owner)
    assert (await client.get(f"/api/jobs/{job.id}", headers=auth_headers(200))).status_code == 404
    assert (await client.get(f"/api/jobs/{uuid.uuid4()}", headers=auth_headers(100))).status_code == 404


async def test_list_jobs_only_own_and_limited(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    mine, other = await make_user(100), await make_user(200)
    for _ in range(3):
        await _job(session, mine)
    await _job(session, other)
    body = (await client.get("/api/jobs", headers=auth_headers(100))).json()
    assert len(body["jobs"]) == 3
    limited = (await client.get("/api/jobs?limit=2", headers=auth_headers(100))).json()
    assert len(limited["jobs"]) == 2
    assert (await client.get("/api/jobs?limit=0", headers=auth_headers(100))).status_code == 422


async def test_health_still_public_and_miniapp_mount_guarded(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/jobs")).status_code == 401
    assert (await client.get("/app/")).status_code in (200, 404)
