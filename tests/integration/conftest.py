"""Fixtures for API tests: real Postgres, fake storage/queue, patched ffprobe, signed initData."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.main import app
from app.api.routers import uploads as uploads_router
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.errors import InvalidMedia
from app.models import User
from app.services.media.probe import ProbeResult
from tests.factories import create_user
from tests.fakes.storage import FakeBlobStorage
from tests.helpers import sign_init_data

BOT_TOKEN = "123456:TEST-TOKEN"


class FakeProbe:
    """Stands in for ffprobe: returns `result`, or raises InvalidMedia when `invalid` is set."""

    def __init__(self) -> None:
        self.result = ProbeResult(
            duration_sec=100.0,
            width=1080,
            height=1920,
            fps=30.0,
            has_audio=True,
            video_codec="h264",
            size_bytes=1000,
            format_name="mov,mp4",
        )
        self.invalid = False
        self.sources: list[str] = []

    async def __call__(self, source: str) -> ProbeResult:
        self.sources.append(source)
        if self.invalid:
            raise InvalidMedia()
        return self.result


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token=BOT_TOKEN,
        max_upload_bytes=10_000_000,
        max_video_duration_sec=600,
        trial_max_duration_sec=60,
        max_active_jobs_per_user=2,
    )


@pytest.fixture
def fake_storage() -> FakeBlobStorage:
    return FakeBlobStorage()


@pytest.fixture
def enqueued() -> list[tuple[Any, ...]]:
    return []


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> FakeProbe:
    fake = FakeProbe()
    monkeypatch.setattr(uploads_router, "probe", fake)
    return fake


@pytest.fixture
async def client(
    session_factory: Callable[[], AsyncSession],
    settings: Settings,
    fake_storage: FakeBlobStorage,
    enqueued: list[tuple[Any, ...]],
    fake_probe: FakeProbe,
) -> AsyncIterator[httpx.AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def fake_enqueue(function_name: str, *args: Any) -> None:
        enqueued.append((function_name, *args))

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[deps.get_storage] = lambda: fake_storage
    app.dependency_overrides[deps.get_enqueue] = lambda: fake_enqueue
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.clear()


def auth_headers(
    telegram_id: int, auth_date: int | None = None, bot_token: str = BOT_TOKEN
) -> dict[str, str]:
    user = {"id": telegram_id, "first_name": f"User{telegram_id}", "username": f"user{telegram_id}"}
    return {"Authorization": "tma " + sign_init_data(user, bot_token, auth_date)}


@pytest.fixture
def make_user(session_factory: Callable[[], AsyncSession]) -> Callable[..., Awaitable[User]]:
    async def _make(
        telegram_id: int = 100,
        balance: int = 5,
        onboarding: bool = True,
        phone: bool = True,
        trial_used: bool = False,
    ) -> User:
        async with session_factory() as session:
            user = await create_user(
                session,
                balance_units=balance,
                telegram_id=telegram_id,
                onboarding_completed=onboarding,
                phone_hash=f"phone-{telegram_id}" if phone else None,
                trial_used=trial_used,
            )
            await session.commit()
            return user

    return _make


@pytest.fixture
def harness(session_factory, tmp_path, clip_two_scenes):  # noqa: ANN001, ANN201
    """WorkerDeps on real Postgres + ffmpeg with fake Gemini/STT/Blob/Notifier (tests/worker_harness.py)."""
    from tests.worker_harness import build_harness

    return build_harness(session_factory, tmp_path, clip_two_scenes)
