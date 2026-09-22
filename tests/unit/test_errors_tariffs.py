import pytest

from app.core import errors
from app.services.tariffs import TARIFFS, get_tariff


def test_error_body_shape_and_messages() -> None:
    err = errors.InsufficientUnits()
    assert err.http_status == 402
    assert err.to_body() == {"error": {"code": "INSUFFICIENT_UNITS", "message_uz": "Birliklar yetarli emas."}}
    assert errors.InvalidState().message_uz == "Amal hozir mumkin emas."
    assert errors.NotFound().message_uz == "Topilmadi."
    assert errors.Forbidden().message_uz == "Ruxsat yo‘q."
    assert errors.TooManyActiveJobs().message_uz == "Faol videolar soni chegaradan oshdi."
    assert errors.TrialUnavailable().message_uz == "Bepul sinov mavjud emas."


def test_error_overrides() -> None:
    err = errors.DomainError("X", "msg", 418)
    assert (err.code, err.message_uz, err.http_status) == ("X", "msg", 418)


def test_tariffs_match_architecture() -> None:
    assert [(t.code, t.units, t.price_uzs) for t in TARIFFS] == [
        ("single", 1, 15_000),
        ("start", 10, 99_000),
        ("pro", 30, 249_000),
        ("max", 100, 690_000),
    ]
    assert [t.code for t in TARIFFS if t.recommended] == ["pro"]
    assert get_tariff("pro").units == 30


def test_unknown_tariff_raises_not_found() -> None:
    with pytest.raises(errors.NotFound):
        get_tariff("nope")
