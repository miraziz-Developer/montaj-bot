"""Shared fixtures. DB tests use a REAL Postgres (TEST_DATABASE_URL), never SQLite."""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import Base


def _new_engine() -> AsyncEngine:
    # NullPool: connections are never reused across event loops (pytest-asyncio uses one loop per test).
    return create_async_engine(get_settings().test_database_url, poolclass=NullPool)


@pytest.fixture(scope="session")
def _schema() -> None:
    async def create() -> None:
        engine = _new_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())


@pytest.fixture
async def engine(_schema: None) -> AsyncIterator[AsyncEngine]:
    engine = _new_engine()
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> Callable[[], AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory: Callable[[], AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


# ---------- synthetic media fixtures (real ffmpeg, tiny clips, no network) ----------

import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

_V264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast"]
_AAC = ["-c:a", "aac"]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("synthetic_media")


@pytest.fixture(scope="session")
def clip_two_scenes(media_dir: Path) -> Path:
    """6 s, 640x360, sine audio; the two 3 s halves look completely different (hard cut at 3 s)."""
    out = media_dir / "two_scenes.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3,hue=h=180,negate",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", *_V264, *_AAC, str(out),
    )  # fmt: skip
    return out


def _with_audio_filter(media_dir: Path, name: str, audio_filter: str) -> Path:
    out = media_dir / name
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-af", audio_filter, *_V264, *_AAC, str(out),
    )  # fmt: skip
    return out


@pytest.fixture(scope="session")
def clip_silent_middle(media_dir: Path) -> Path:
    """6 s clip whose audio is digital silence between 2 s and 3 s."""
    return _with_audio_filter(media_dir, "silent_middle.mp4", "volume=enable='between(t,2,3)':volume=0")


@pytest.fixture(scope="session")
def clip_silent_tail(media_dir: Path) -> Path:
    """6 s clip whose audio goes silent at 4.5 s and stays silent until the end (silence open at EOF)."""
    return _with_audio_filter(media_dir, "silent_tail.mp4", "volume=enable='gte(t,4.5)':volume=0")


@pytest.fixture(scope="session")
def clip_continuous(media_dir: Path) -> Path:
    return _with_audio_filter(media_dir, "continuous.mp4", "anull")


@pytest.fixture(scope="session")
def clip_no_audio(media_dir: Path) -> Path:
    out = media_dir / "no_audio.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3", *_V264, str(out))
    return out


@pytest.fixture(scope="session")
def clip_portrait(media_dir: Path) -> Path:
    out = media_dir / "portrait.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=2", *_V264, str(out))
    return out


@pytest.fixture(scope="session")
def clip_static_20s(media_dir: Path) -> Path:
    """20 s of one flat colour: PySceneDetect finds zero cuts."""
    out = media_dir / "static_20s.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=15:duration=20", *_V264, str(out))
    return out
