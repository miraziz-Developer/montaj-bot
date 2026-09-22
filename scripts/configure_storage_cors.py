"""Create the blob containers (if missing) and set service-level CORS. Idempotent.

Usage: docker compose run --rm api python scripts/configure_storage_cors.py
"""

import asyncio

from azure.storage.blob import CorsRule
from azure.storage.blob.aio import BlobServiceClient

from app.core.config import Settings, get_settings
from app.services.storage import ensure_containers


def build_cors_rule(settings: Settings) -> CorsRule:
    origins = [settings.public_base_url.rstrip("/")]
    if settings.env == "dev":
        origins.append("*")
    return CorsRule(
        allowed_origins=origins,
        allowed_methods=["GET", "PUT", "HEAD", "OPTIONS"],
        allowed_headers=["*"],
        exposed_headers=["*"],
        max_age_in_seconds=3600,
    )


async def main() -> None:
    settings = get_settings()
    created = await ensure_containers(settings)
    print(f"containers created: {created or 'none (already existed)'}")
    async with BlobServiceClient.from_connection_string(settings.azure_storage_connection_string) as service:
        await service.set_service_properties(cors=[build_cors_rule(settings)])
    print("CORS configured")


if __name__ == "__main__":
    asyncio.run(main())
