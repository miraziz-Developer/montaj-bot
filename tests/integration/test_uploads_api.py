import time
import uuid
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, Upload, User
from app.models.enums import JobStatus, SourceRole, UploadStatus
from app.models.job_source import JobSource
from tests.factories import create_job
from tests.fakes.storage import FakeBlobStorage
from tests.integration.conftest import FakeProbe, auth_headers

MakeUser = Callable[..., Awaitable[User]]
SIZE = 1000


async def _init(client: httpx.AsyncClient, tg_id: int, *, filename: str = "clip.mp4", size: int = SIZE):
    return await client.post(
        "/api/uploads/init",
        json={"filename": filename, "size_bytes": size, "content_type": "video/mp4"},
        headers=auth_headers(tg_id),
    )


async def _init_and_put(
    client: httpx.AsyncClient, storage: FakeBlobStorage, user: User, *, blob_size: int = SIZE
) -> str:
    response = await _init(client, user.telegram_id)
    assert response.status_code == 200, response.text
    upload_id = response.json()["upload_id"]
    storage.put("uploads", f"{user.id}/{upload_id}/source.mp4", b"x" * blob_size)
    return upload_id


# ---------- auth ----------


async def test_missing_or_bad_auth_is_401(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100)
    cases = [
        {},
        {"Authorization": "Bearer abc"},
        {"Authorization": "tma "},
        {"Authorization": "tma garbage"},
        auth_headers(100, bot_token="999:WRONG"),
        auth_headers(100, auth_date=int(time.time()) - 90000),
    ]
    for headers in cases:
        response = await client.get("/api/me", headers=headers)
        assert response.status_code == 401, headers
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


async def test_valid_init_data_creates_unknown_user(client: httpx.AsyncClient, session: AsyncSession) -> None:
    response = await client.get("/api/me", headers=auth_headers(555))
    assert response.status_code == 200
    assert response.json()["onboarding_completed"] is False
    assert await session.scalar(select(func.count()).select_from(User).where(User.telegram_id == 555)) == 1


async def test_me_shape(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100, balance=4)
    body = (await client.get("/api/me", headers=auth_headers(100))).json()
    assert body["telegram_id"] == 100
    assert body["balance_units"] == 4
    assert body["trial_available"] is True
    assert [t["code"] for t in body["tariffs"]] == ["single", "start", "pro", "max"]
    assert {"code", "units", "price_uzs", "label"} <= set(body["tariffs"][0])


async def test_trial_needs_phone(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100, phone=False)
    assert (await client.get("/api/me", headers=auth_headers(100))).json()["trial_available"] is False


# ---------- init ----------


async def test_init_ok(client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession) -> None:
    user = await make_user(100)
    response = await _init(client, 100, filename="My Clip.MOV")
    assert response.status_code == 200
    body = response.json()
    assert body["block_size"] == 8388608 and body["max_parallel"] == 4
    assert body["upload_url"].startswith("fake://upload/uploads/")
    upload = await session.get(Upload, uuid.UUID(body["upload_id"]))
    assert upload.status == UploadStatus.INIT
    assert upload.blob_path == f"{user.id}/{upload.id}/source.mov"


async def test_init_requires_onboarding(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100, onboarding=False)
    response = await _init(client, 100)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "ONBOARDING_REQUIRED")


async def test_init_no_credits(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100, balance=0, trial_used=True)
    response = await _init(client, 100)
    assert (response.status_code, response.json()["error"]["code"]) == (402, "NO_CREDITS")


async def test_init_allowed_with_trial_only(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100, balance=0)
    assert (await _init(client, 100)).status_code == 200


async def test_init_file_too_large(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100)
    response = await _init(client, 100, size=10_000_001)
    assert (response.status_code, response.json()["error"]["code"]) == (413, "FILE_TOO_LARGE")


async def test_init_unsupported_extension(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100)
    for name in ("notes.txt", "noext", "evil.mp4.exe"):
        response = await _init(client, 100, filename=name)
        assert (response.status_code, response.json()["error"]["code"]) == (415, "UNSUPPORTED_TYPE"), name


async def test_init_rejects_bad_body(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100)
    response = await _init(client, 100, size=0)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "VALIDATION_ERROR")


async def test_init_too_many_active_jobs(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    user = await make_user(100)
    await create_job(session, user.id, status=JobStatus.AWAITING_CONFIRM)
    await create_job(session, user.id, status=JobStatus.RENDERING)
    await create_job(session, user.id, status=JobStatus.DONE)  # terminal: does not count
    await session.commit()
    response = await _init(client, 100)
    assert (response.status_code, response.json()["error"]["code"]) == (429, "TOO_MANY_ACTIVE_JOBS")


# ---------- ownership, resume, blocks ----------


async def test_other_user_gets_404(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage
) -> None:
    owner = await make_user(100)
    await make_user(200)
    upload_id = await _init_and_put(client, fake_storage, owner)
    for method, suffix in (("post", "resume"), ("get", "blocks"), ("post", "complete")):
        response = await getattr(client, method)(
            f"/api/uploads/{upload_id}/{suffix}", headers=auth_headers(200)
        )
        assert (response.status_code, response.json()["error"]["code"]) == (404, "NOT_FOUND"), suffix


async def test_unknown_upload_is_404(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    await make_user(100)
    response = await client.post(f"/api/uploads/{uuid.uuid4()}/resume", headers=auth_headers(100))
    assert response.status_code == 404


async def test_resume_and_blocks(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage
) -> None:
    user = await make_user(100)
    upload_id = (await _init(client, 100)).json()["upload_id"]
    fake_storage.uncommitted[("uploads", f"{user.id}/{upload_id}/source.mp4")] = ["MDAwMDAw", "MDAwMDAx"]

    resumed = await client.post(f"/api/uploads/{upload_id}/resume", headers=auth_headers(100))
    assert resumed.status_code == 200
    assert resumed.json()["upload_id"] == upload_id and "upload_url" in resumed.json()

    blocks = await client.get(f"/api/uploads/{upload_id}/blocks", headers=auth_headers(100))
    assert blocks.json() == {"uploaded_block_ids": ["MDAwMDAw", "MDAwMDAx"], "block_size": 8388608}


async def test_resume_after_verified_is_409(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage, fake_probe: FakeProbe
) -> None:
    user = await make_user(100)
    upload_id = await _init_and_put(client, fake_storage, user)
    assert (
        await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))
    ).status_code == 200
    for method, suffix in (("post", "resume"), ("get", "blocks")):
        response = await getattr(client, method)(
            f"/api/uploads/{upload_id}/{suffix}", headers=auth_headers(100)
        )
        assert (response.status_code, response.json()["error"]["code"]) == (409, "INVALID_STATE")


# ---------- complete ----------


async def test_complete_success(
    client: httpx.AsyncClient,
    make_user: MakeUser,
    fake_storage: FakeBlobStorage,
    fake_probe: FakeProbe,
    session: AsyncSession,
) -> None:
    user = await make_user(100, balance=5)
    upload_id = await _init_and_put(client, fake_storage, user)
    response = await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duration_sec"] == 100.0 and (body["width"], body["height"]) == (1080, 1920)
    assert (body["units_cost"], body["is_trial"]) == (1, False)
    assert (body["balance_units"], body["balance_after"]) == (5, 4)

    upload = await session.get(Upload, uuid.UUID(upload_id))
    assert upload.status == UploadStatus.VERIFIED and upload.has_audio is True
    job = await session.get(Job, uuid.UUID(body["job_id"]))
    assert job.status == JobStatus.AWAITING_CONFIRM
    assert (
        fake_probe.sources and "public=False" in fake_probe.sources[0]
    )  # server-side URL, not the browser one


async def test_complete_units_scale_with_duration(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage, fake_probe: FakeProbe
) -> None:
    from dataclasses import replace

    user = await make_user(100)
    fake_probe.result = replace(fake_probe.result, duration_sec=400.0)
    upload_id = await _init_and_put(client, fake_storage, user)
    body = (await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))).json()
    assert (body["units_cost"], body["balance_after"]) == (3, 2)


async def test_complete_is_idempotent(
    client: httpx.AsyncClient,
    make_user: MakeUser,
    fake_storage: FakeBlobStorage,
    fake_probe: FakeProbe,
    session: AsyncSession,
) -> None:
    user = await make_user(100)
    upload_id = await _init_and_put(client, fake_storage, user)
    first = (await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))).json()
    second = (await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))).json()
    assert first == second
    assert await session.scalar(select(func.count()).select_from(Job)) == 1
    assert len(fake_probe.sources) == 1


async def test_complete_blob_missing(
    client: httpx.AsyncClient, make_user: MakeUser, session: AsyncSession
) -> None:
    await make_user(100)
    upload_id = (await _init(client, 100)).json()["upload_id"]
    response = await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))
    assert (response.status_code, response.json()["error"]["code"]) == (400, "BLOB_MISSING")
    assert (await session.get(Upload, uuid.UUID(upload_id))).status == UploadStatus.INIT


async def test_complete_size_mismatch_rejects_and_deletes_blob(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage, session: AsyncSession
) -> None:
    user = await make_user(100)
    upload_id = await _init_and_put(client, fake_storage, user, blob_size=SIZE - 1)
    response = await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))
    assert (response.status_code, response.json()["error"]["code"]) == (422, "SIZE_MISMATCH")
    assert (await session.get(Upload, uuid.UUID(upload_id))).status == UploadStatus.REJECTED
    assert not fake_storage.blobs
    assert await session.scalar(select(func.count()).select_from(Job)) == 0


async def test_complete_too_long(
    client: httpx.AsyncClient,
    make_user: MakeUser,
    fake_storage: FakeBlobStorage,
    fake_probe: FakeProbe,
    session: AsyncSession,
) -> None:
    from dataclasses import replace

    user = await make_user(100)
    fake_probe.result = replace(fake_probe.result, duration_sec=601.0)
    upload_id = await _init_and_put(client, fake_storage, user)
    response = await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))
    assert (response.status_code, response.json()["error"]["code"]) == (422, "TOO_LONG")
    assert (await session.get(Upload, uuid.UUID(upload_id))).status == UploadStatus.REJECTED


async def test_complete_invalid_media(
    client: httpx.AsyncClient,
    make_user: MakeUser,
    fake_storage: FakeBlobStorage,
    fake_probe: FakeProbe,
    session: AsyncSession,
) -> None:
    user = await make_user(100)
    fake_probe.invalid = True
    upload_id = await _init_and_put(client, fake_storage, user)
    response = await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))
    assert (response.status_code, response.json()["error"]["code"]) == (422, "INVALID_MEDIA")
    assert (await session.get(Upload, uuid.UUID(upload_id))).status == UploadStatus.REJECTED


async def test_complete_trial_too_long_states_limit(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage, fake_probe: FakeProbe
) -> None:
    from dataclasses import replace

    user = await make_user(100, balance=0)
    fake_probe.result = replace(fake_probe.result, duration_sec=61.0)
    upload_id = await _init_and_put(client, fake_storage, user)
    response = await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))
    body = response.json()["error"]
    assert (response.status_code, body["code"]) == (422, "TRIAL_TOO_LONG")
    assert "60" in body["message_uz"]


async def test_complete_trial_success(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage, fake_probe: FakeProbe
) -> None:
    from dataclasses import replace

    user = await make_user(100, balance=0)
    fake_probe.result = replace(fake_probe.result, duration_sec=45.0)
    upload_id = await _init_and_put(client, fake_storage, user)
    body = (await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))).json()
    assert (body["is_trial"], body["balance_units"], body["balance_after"]) == (True, 0, 0)


async def test_long_video_ok_for_paying_user_even_if_trial_limit_exceeded(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage, fake_probe: FakeProbe
) -> None:
    user = await make_user(
        100, balance=3
    )  # default probe is 100 s: over the 60 s trial limit, must not apply
    upload_id = await _init_and_put(client, fake_storage, user)
    response = await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(100))
    assert response.status_code == 200 and response.json()["is_trial"] is False


# ---------- attach (P13 multi-source B-roll) ----------


async def _confirmed_job(
    client: httpx.AsyncClient, storage: FakeBlobStorage, user: User, *, balance: int = 5
) -> dict:
    upload_id = await _init_and_put(client, storage, user, blob_size=SIZE)
    response = await client.post(f"/api/uploads/{upload_id}/complete", headers=auth_headers(user.telegram_id))
    assert response.status_code == 200, response.text
    return response.json()


async def test_attach_broll_success(
    client: httpx.AsyncClient,
    make_user: MakeUser,
    fake_storage: FakeBlobStorage,
    fake_probe: FakeProbe,
    session: AsyncSession,
) -> None:
    user = await make_user(100, balance=5)
    primary = await _confirmed_job(client, fake_storage, user)
    broll_id = await _init_and_put(client, fake_storage, user)

    response = await client.post(
        f"/api/uploads/{broll_id}/attach",
        json={"job_id": primary["job_id"]},
        headers=auth_headers(100),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["broll_count"] == 1
    assert body["units_cost"] == primary["units_cost"] + 1

    upload = await session.get(Upload, uuid.UUID(broll_id))
    assert upload.status == UploadStatus.VERIFIED
    count = await session.scalar(
        select(func.count())
        .select_from(JobSource)
        .where(
            JobSource.job_id == uuid.UUID(primary["job_id"]),
            JobSource.role == SourceRole.BROLL,
        )
    )
    assert count == 1


async def test_attach_wrong_job_owner_is_404(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage, fake_probe: FakeProbe
) -> None:
    owner = await make_user(100, balance=5)
    other = await make_user(200)
    primary = await _confirmed_job(client, fake_storage, owner)
    broll_id = await _init_and_put(client, fake_storage, other)

    response = await client.post(
        f"/api/uploads/{broll_id}/attach",
        json={"job_id": primary["job_id"]},
        headers=auth_headers(200),
    )
    assert (response.status_code, response.json()["error"]["code"]) == (404, "NOT_FOUND")


async def test_attach_requires_awaiting_confirm(
    client: httpx.AsyncClient,
    make_user: MakeUser,
    fake_storage: FakeBlobStorage,
    fake_probe: FakeProbe,
    session: AsyncSession,
) -> None:
    user = await make_user(100, balance=5)
    primary = await _confirmed_job(client, fake_storage, user)
    job = await session.get(Job, uuid.UUID(primary["job_id"]))
    job.status = JobStatus.QUEUED
    await session.commit()
    broll_id = await _init_and_put(client, fake_storage, user)

    response = await client.post(
        f"/api/uploads/{broll_id}/attach",
        json={"job_id": primary["job_id"]},
        headers=auth_headers(100),
    )
    assert (response.status_code, response.json()["error"]["code"]) == (409, "INVALID_STATE")


async def test_attach_rejects_past_the_broll_cap(
    client: httpx.AsyncClient, make_user: MakeUser, fake_storage: FakeBlobStorage, fake_probe: FakeProbe
) -> None:
    user = await make_user(100, balance=5)
    primary = await _confirmed_job(client, fake_storage, user)
    for _ in range(4):  # default cap: settings.max_broll_sources_per_job == 4
        broll_id = await _init_and_put(client, fake_storage, user)
        ok = await client.post(
            f"/api/uploads/{broll_id}/attach",
            json={"job_id": primary["job_id"]},
            headers=auth_headers(100),
        )
        assert ok.status_code == 200, ok.text

    one_too_many = await _init_and_put(client, fake_storage, user)
    response = await client.post(
        f"/api/uploads/{one_too_many}/attach",
        json={"job_id": primary["job_id"]},
        headers=auth_headers(100),
    )
    assert (response.status_code, response.json()["error"]["code"]) == (429, "TOO_MANY_BROLL_SOURCES")
