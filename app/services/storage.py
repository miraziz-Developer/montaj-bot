"""Blob storage behind a Protocol. Production: Azure Blob (Azurite in dev). Tests: tests/fakes/storage.py."""

import asyncio
import base64
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions, ContentSettings, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from app.core.config import Settings

logger = logging.getLogger(__name__)


class BlobStorage(Protocol):
    async def create_upload_url(self, container: str, blob_path: str, ttl_min: int) -> str:
        """Write-only (create+write) SAS URL for ONE blob."""
        ...

    async def list_uncommitted_block_ids(self, container: str, blob_path: str) -> list[str]:
        """Uncommitted block ids in the base64 form the client used in Put Block."""
        ...

    async def get_blob_size(self, container: str, blob_path: str) -> int | None:
        """Committed blob size in bytes, or None if the blob does not exist."""
        ...

    async def create_read_url(
        self, container: str, blob_path: str, ttl_hours: float, *, public: bool = True
    ) -> str:
        """Read-only SAS URL. public=False uses the server-side endpoint (e.g. for ffprobe in Docker)."""
        ...

    async def download_to_file(self, container: str, blob_path: str, dest: Path) -> None: ...

    async def upload_file(self, container: str, blob_path: str, src: Path, content_type: str) -> None: ...

    async def delete_blob(self, container: str, blob_path: str) -> None: ...

    async def delete_prefix(self, container: str, prefix: str) -> None: ...

    async def list_old_blobs(self, container: str, older_than: timedelta) -> list[str]: ...


def _parse_connection_string(conn: str) -> dict[str, str]:
    parts: dict[str, str] = {}
    for item in conn.split(";"):
        key, sep, value = item.partition("=")  # AccountKey ends with '=' padding: split on the first '='
        if sep:
            parts[key.strip()] = value.strip()
    return parts


class AzureBlobStorage:
    def __init__(self, settings: Settings) -> None:
        self._conn = settings.azure_storage_connection_string
        parts = _parse_connection_string(self._conn)
        try:
            self._account_name = parts["AccountName"]
            self._account_key = parts["AccountKey"]
        except KeyError:
            raise ValueError(
                "AZURE_STORAGE_CONNECTION_STRING must contain AccountName and AccountKey"
            ) from None
        protocol = parts.get("DefaultEndpointsProtocol", "https")
        suffix = parts.get("EndpointSuffix", "core.windows.net")
        self._endpoint = (
            parts.get("BlobEndpoint") or f"{protocol}://{self._account_name}.blob.{suffix}"
        ).rstrip("/")
        self._public_endpoint = settings.azure_public_blob_endpoint.rstrip("/") or self._endpoint

    def _client(self) -> BlobServiceClient:
        return BlobServiceClient.from_connection_string(self._conn)

    def _sas_url(
        self,
        container: str,
        blob_path: str,
        permission: BlobSasPermissions,
        expiry: datetime,
        *,
        public: bool,
    ) -> str:
        sas = generate_blob_sas(
            account_name=self._account_name,
            container_name=container,
            blob_name=blob_path,
            account_key=self._account_key,
            permission=permission,
            expiry=expiry,
        )
        base = self._public_endpoint if public else self._endpoint
        return f"{base}/{container}/{quote(blob_path)}?{sas}"

    async def create_upload_url(self, container: str, blob_path: str, ttl_min: int) -> str:
        expiry = datetime.now(UTC) + timedelta(minutes=ttl_min)
        return self._sas_url(
            container, blob_path, BlobSasPermissions(create=True, write=True), expiry, public=True
        )

    async def create_read_url(
        self, container: str, blob_path: str, ttl_hours: float, *, public: bool = True
    ) -> str:
        expiry = datetime.now(UTC) + timedelta(hours=ttl_hours)
        return self._sas_url(container, blob_path, BlobSasPermissions(read=True), expiry, public=public)

    async def list_uncommitted_block_ids(self, container: str, blob_path: str) -> list[str]:
        async with self._client() as service:
            blob = service.get_blob_client(container, blob_path)
            try:
                # Verified against Azurite: returns (committed_blocks, uncommitted_blocks)
                _, uncommitted = await blob.get_block_list("uncommitted")
            except ResourceNotFoundError:
                return []
            # The SDK base64-DECODES block ids; clients (Mini App) send and expect the base64 form.
            return [base64.b64encode(block.id.encode()).decode() for block in uncommitted]

    async def get_blob_size(self, container: str, blob_path: str) -> int | None:
        async with self._client() as service:
            blob = service.get_blob_client(container, blob_path)
            try:
                props = await blob.get_blob_properties()
            except ResourceNotFoundError:
                return None
            return props.size

    async def download_to_file(self, container: str, blob_path: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self._client() as service:
            stream = await service.get_blob_client(container, blob_path).download_blob()
            with dest.open("wb") as handle:
                async for chunk in stream.chunks():
                    await asyncio.to_thread(handle.write, chunk)

    async def upload_file(self, container: str, blob_path: str, src: Path, content_type: str) -> None:
        async with self._client() as service:
            blob = service.get_blob_client(container, blob_path)
            with src.open("rb") as handle:
                await blob.upload_blob(
                    handle, overwrite=True, content_settings=ContentSettings(content_type=content_type)
                )

    async def delete_blob(self, container: str, blob_path: str) -> None:
        async with self._client() as service:
            try:
                await service.get_blob_client(container, blob_path).delete_blob()
            except ResourceNotFoundError:
                pass

    async def delete_prefix(self, container: str, prefix: str) -> None:
        async with self._client() as service:
            container_client = service.get_container_client(container)
            names = [blob.name async for blob in container_client.list_blobs(name_starts_with=prefix)]
            for name in names:
                try:
                    await container_client.delete_blob(name)
                except ResourceNotFoundError:
                    pass

    async def list_old_blobs(self, container: str, older_than: timedelta) -> list[str]:
        cutoff = datetime.now(UTC) - older_than
        async with self._client() as service:
            container_client = service.get_container_client(container)
            return [
                blob.name
                async for blob in container_client.list_blobs()
                if blob.last_modified is not None and blob.last_modified < cutoff
            ]


async def ensure_containers(settings: Settings) -> list[str]:
    """Create the three private containers if missing. Returns the names created."""
    created: list[str] = []
    names = (
        settings.azure_uploads_container,
        settings.azure_artifacts_container,
        settings.azure_outputs_container,
    )
    async with BlobServiceClient.from_connection_string(settings.azure_storage_connection_string) as service:
        for name in names:
            try:
                await service.create_container(name)
                created.append(name)
            except ResourceExistsError:
                pass
    return created
