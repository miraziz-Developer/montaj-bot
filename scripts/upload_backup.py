"""Upload one database backup to the PRIVATE backup blob container (created if missing).

Runs inside the api image so the server needs no Azure CLI:

    docker compose ... run --rm -T -v /path/dump.sql.gz:/backup/dump.sql.gz:ro api \
        python scripts/upload_backup.py /backup/dump.sql.gz montaj_20260101T031500Z.sql.gz
"""

import asyncio
import os
import sys
from pathlib import Path

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob.aio import BlobServiceClient


async def main(path: Path, blob_name: str) -> None:
    connection_string = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    container = os.environ.get("AZURE_BACKUP_CONTAINER", "backups")
    async with BlobServiceClient.from_connection_string(connection_string) as service:
        try:
            await service.create_container(container)  # private by default
        except ResourceExistsError:
            pass
        blob = service.get_blob_client(container, blob_name)
        with path.open("rb") as handle:
            await blob.upload_blob(handle, overwrite=True)
    print(f"uploaded {blob_name} to container {container}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: upload_backup.py <file> <blob-name>")
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
