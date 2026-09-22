"""AzureBlobStorage against a real Azurite (docker compose `azurite`). Skipped if it is not reachable."""

import base64
import socket
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.services.storage import AzureBlobStorage, _parse_connection_string, ensure_containers


def _azurite_endpoint() -> str | None:
    conn = get_settings().azure_storage_connection_string
    endpoint = _parse_connection_string(conn).get("BlobEndpoint")
    if not endpoint:
        return None
    parsed = urlparse(endpoint)
    try:
        socket.create_connection((parsed.hostname, parsed.port or 80), timeout=1).close()
    except OSError:
        return None
    return endpoint


pytestmark = pytest.mark.skipif(_azurite_endpoint() is None, reason="Azurite is not reachable")


def _settings(**overrides: str) -> Settings:
    conn = get_settings().azure_storage_connection_string
    # Default the public endpoint to "" so a dev .env (browser-facing localhost URL) cannot leak in.
    overrides.setdefault("azure_public_blob_endpoint", "")
    return Settings(_env_file=None, azure_storage_connection_string=conn, **overrides)


@pytest.fixture
async def storage() -> AzureBlobStorage:
    settings = _settings()
    await ensure_containers(settings)
    return AzureBlobStorage(settings)


def _block_id(index: int) -> str:
    return base64.b64encode(f"{index:06d}".encode()).decode()


async def test_block_upload_flow_and_sas_permissions(storage: AzureBlobStorage, tmp_path: Path) -> None:
    path = f"test/{uuid.uuid4()}/source.mp4"
    data = b"0123456789" * 100
    upload_url = await storage.create_upload_url("uploads", path, ttl_min=5)
    assert await storage.get_blob_size("uploads", path) is None

    async with httpx.AsyncClient() as http:
        block = _block_id(0)
        put = await http.put(f"{upload_url}&comp=block&blockid={block}", content=data)
        assert put.status_code == 201, put.text
        assert await storage.list_uncommitted_block_ids("uploads", path) == [block]
        assert await storage.get_blob_size("uploads", path) is None  # not committed yet

        xml = f"<BlockList><Latest>{block}</Latest></BlockList>"
        commit = await http.put(f"{upload_url}&comp=blocklist", content=xml)
        assert commit.status_code == 201, commit.text

        # The upload URL is write-only: it must NOT allow reading the blob back.
        assert (await http.get(upload_url)).status_code == 403

        read_url = await storage.create_read_url("uploads", path, ttl_hours=1, public=False)
        assert (await http.get(read_url)).content == data
        # ...and the read URL must not allow writing.
        blocked = await http.put(f"{read_url}&comp=block&blockid={_block_id(1)}", content=b"x")
        assert blocked.status_code == 403

    assert await storage.get_blob_size("uploads", path) == len(data)
    dest = tmp_path / "out" / "copy.mp4"
    await storage.download_to_file("uploads", path, dest)
    assert dest.read_bytes() == data

    await storage.delete_blob("uploads", path)
    await storage.delete_blob("uploads", path)  # deleting a missing blob is not an error
    assert await storage.get_blob_size("uploads", path) is None


async def test_upload_file_list_old_and_delete_prefix(storage: AzureBlobStorage, tmp_path: Path) -> None:
    job_id = uuid.uuid4()
    src = tmp_path / "proxy.mp4"
    src.write_bytes(b"proxy-bytes")
    for name in ("proxy.mp4", "analysis.json"):
        await storage.upload_file("artifacts", f"{job_id}/{name}", src, "application/octet-stream")

    assert await storage.get_blob_size("artifacts", f"{job_id}/proxy.mp4") == len(b"proxy-bytes")
    old = await storage.list_old_blobs("artifacts", timedelta(seconds=-60))  # cutoff in the future
    assert {f"{job_id}/proxy.mp4", f"{job_id}/analysis.json"} <= set(old)
    assert f"{job_id}/proxy.mp4" not in await storage.list_old_blobs("artifacts", timedelta(days=2))

    await storage.delete_prefix("artifacts", f"{job_id}/")
    assert await storage.get_blob_size("artifacts", f"{job_id}/proxy.mp4") is None
    assert await storage.get_blob_size("artifacts", f"{job_id}/analysis.json") is None


async def test_public_endpoint_only_rewrites_public_urls() -> None:
    storage = AzureBlobStorage(
        _settings(azure_public_blob_endpoint="http://localhost:10000/devstoreaccount1/")
    )
    assert (await storage.create_upload_url("uploads", "a/b.mp4", 5)).startswith(
        "http://localhost:10000/devstoreaccount1/uploads/a/b.mp4?"
    )
    server_side = await storage.create_read_url("uploads", "a/b.mp4", 1, public=False)
    assert server_side.startswith("http://azurite:10000/devstoreaccount1/uploads/a/b.mp4?")
