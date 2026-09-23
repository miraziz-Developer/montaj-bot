from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (case-insensitive names)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://montaj:montaj@db:5432/montaj"
    test_database_url: str = "postgresql+asyncpg://montaj:montaj@db:5432/montaj_test"
    redis_url: str = "redis://redis:6379/0"

    bot_token: str = ""
    bot_username: str = ""
    telegram_api_base: str = "http://telegram-bot-api:8081"
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    # NoDecode: pydantic-settings must not JSON-decode "1,2"; the validator below parses it.
    admin_telegram_ids: Annotated[list[int], NoDecode] = []
    public_base_url: str = "http://localhost:8000"
    phone_hash_pepper: str = "change-me"

    azure_storage_connection_string: str = ""
    azure_uploads_container: str = "uploads"
    azure_artifacts_container: str = "artifacts"
    azure_outputs_container: str = "outputs"
    # Browser-reachable blob endpoint incl. account path (Azurite: http://localhost:10000/devstoreaccount1).
    # Empty = use the endpoint from the connection string.
    azure_public_blob_endpoint: str = ""
    upload_sas_ttl_min: int = 120
    download_sas_ttl_hours: int = 48

    max_upload_bytes: int = 3_221_225_472
    max_video_duration_sec: int = 3600
    trial_max_duration_sec: int = 60
    unit_seconds: int = 180
    free_revisions_per_job: int = 2
    revision_cost_units: int = 1
    max_active_jobs_per_user: int = 2
    max_broll_sources_per_job: int = 4
    broll_surcharge_units: int = 1

    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_analysis_model: str = ""
    gemini_planner_model: str = ""
    gemini_analysis_fps: float = 1.0
    gemini_analysis_fps_long: float = 0.5
    gemini_media_resolution: str = "low"
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_analysis_deployment: str = ""
    azure_planner_deployment: str = ""
    llm_max_concurrency: int = 2
    analysis_chunk_sec: int = 600
    long_video_threshold_sec: int = 1200

    stt_provider: str = "groq"
    groq_api_key: str = ""
    groq_stt_model: str = "whisper-large-v3-turbo"
    azure_speech_api_key: str = ""
    azure_speech_endpoint: str = ""
    azure_speech_locales: str = "uz-UZ,ru-RU"
    stt_concurrency: int = 3

    worker_concurrency: int = 1
    job_timeout_sec: int = 14400
    render_preset: str = "veryfast"
    render_crf: int = 21
    render_clip_concurrency: int = 2
    deliver_max_inline_bytes: int = 1_900_000_000  # bigger results (or failed sends) become a 48 h link
    tmp_dir: str = "/tmp/montaj"
    assets_dir: str = "/app/assets"

    price_gemini_in_per_m_usd: float = 0.30
    price_gemini_out_per_m_usd: float = 2.50
    price_stt_per_hour_usd: float = 0.04
    price_vm_per_hour_usd: float = 0.10
    payment_card_text: str = ""

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
