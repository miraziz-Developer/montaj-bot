import httpx
import pytest

from app.api.main import app
from app.api.routers import health


async def _get_health() -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health")


async def test_health_ok_against_real_services() -> None:
    """Needs the compose network (db + redis); run via `make test`."""
    response = await _get_health()
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True, "redis": True}


@pytest.mark.parametrize(("db_ok", "redis_ok"), [(False, True), (True, False), (False, False)])
async def test_health_degraded_returns_503(
    monkeypatch: pytest.MonkeyPatch, db_ok: bool, redis_ok: bool
) -> None:
    async def fake_db() -> bool:
        return db_ok

    async def fake_redis() -> bool:
        return redis_ok

    monkeypatch.setattr(health, "check_db", fake_db)
    monkeypatch.setattr(health, "check_redis", fake_redis)

    response = await _get_health()
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": db_ok, "redis": redis_ok}
