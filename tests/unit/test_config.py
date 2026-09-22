import pytest

from app.core.config import Settings


def test_defaults_load(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:  # a dev .env / container env must not leak into the defaults check
        monkeypatch.delenv(name.upper(), raising=False)
    settings = Settings(_env_file=None)
    assert settings.unit_seconds == 180
    assert settings.max_upload_bytes == 3_221_225_472
    assert settings.render_preset == "veryfast"
    assert settings.admin_telegram_ids == []
    assert settings.bot_token == ""


def test_admin_ids_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "1,2")
    assert Settings(_env_file=None).admin_telegram_ids == [1, 2]


def test_admin_ids_ignore_blanks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", " 5, ,7 ")
    assert Settings(_env_file=None).admin_telegram_ids == [5, 7]
