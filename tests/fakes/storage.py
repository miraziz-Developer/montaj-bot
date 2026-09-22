from datetime import UTC, datetime, timedelta
from pathlib import Path


class FakeBlobStorage:
    """In-memory BlobStorage. Tests 'upload' with put(); URLs are fake:// strings."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.modified: dict[tuple[str, str], datetime] = {}
        self.uncommitted: dict[tuple[str, str], list[str]] = {}
        self.upload_urls: list[tuple[str, str, int]] = []
        self.read_urls: list[tuple[str, str, float, bool]] = []  # (container, path, ttl_hours, public)
        self.downloads: list[tuple[str, str]] = []  # (container, blob_path) of every download_to_file

    def put(self, container: str, blob_path: str, data: bytes, modified: datetime | None = None) -> None:
        self.blobs[(container, blob_path)] = data
        self.modified[(container, blob_path)] = modified or datetime.now(UTC)

    async def create_upload_url(self, container: str, blob_path: str, ttl_min: int) -> str:
        self.upload_urls.append((container, blob_path, ttl_min))
        return f"fake://upload/{container}/{blob_path}?perm=cw&ttl_min={ttl_min}"

    async def list_uncommitted_block_ids(self, container: str, blob_path: str) -> list[str]:
        return list(self.uncommitted.get((container, blob_path), []))

    async def get_blob_size(self, container: str, blob_path: str) -> int | None:
        data = self.blobs.get((container, blob_path))
        return None if data is None else len(data)

    async def create_read_url(
        self, container: str, blob_path: str, ttl_hours: float, *, public: bool = True
    ) -> str:
        self.read_urls.append((container, blob_path, ttl_hours, public))
        return f"fake://read/{container}/{blob_path}?perm=r&ttl_h={ttl_hours}&public={public}"

    async def download_to_file(self, container: str, blob_path: str, dest: Path) -> None:
        self.downloads.append((container, blob_path))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blobs[(container, blob_path)])

    async def upload_file(self, container: str, blob_path: str, src: Path, content_type: str) -> None:
        self.put(container, blob_path, src.read_bytes())

    async def delete_blob(self, container: str, blob_path: str) -> None:
        self.blobs.pop((container, blob_path), None)
        self.modified.pop((container, blob_path), None)

    async def delete_prefix(self, container: str, prefix: str) -> None:
        for key in [k for k in self.blobs if k[0] == container and k[1].startswith(prefix)]:
            await self.delete_blob(*key)

    async def list_old_blobs(self, container: str, older_than: timedelta) -> list[str]:
        cutoff = datetime.now(UTC) - older_than
        return [path for (c, path), when in self.modified.items() if c == container and when < cutoff]
