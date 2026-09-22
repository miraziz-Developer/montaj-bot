import pytest

from app.services.units import compute_units


@pytest.mark.parametrize(
    ("duration_sec", "expected"),
    [(0.5, 1), (0, 1), (180, 1), (180.1, 2), (360, 2), (360.1, 3), (3600, 20)],
)
def test_compute_units(duration_sec: float, expected: int) -> None:
    assert compute_units(duration_sec, 180) == expected


def test_compute_units_respects_unit_seconds() -> None:
    assert compute_units(100, 60) == 2
