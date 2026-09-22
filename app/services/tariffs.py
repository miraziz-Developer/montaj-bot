from dataclasses import dataclass

from app.core.errors import NotFound


@dataclass(frozen=True, slots=True)
class Tariff:
    code: str
    label: str
    units: int
    price_uzs: int
    recommended: bool = False


REFERRAL_BONUS_UNITS = 3  # inviter reward after the invited user's FIRST job is DONE (ARCHITECTURE section 8)

TARIFFS: list[Tariff] = [
    Tariff(code="single", label="Bitta video", units=1, price_uzs=15_000),
    Tariff(code="start", label="Start", units=10, price_uzs=99_000),
    Tariff(code="pro", label="Pro", units=30, price_uzs=249_000, recommended=True),
    Tariff(code="max", label="Max", units=100, price_uzs=690_000),
]


def get_tariff(code: str) -> Tariff:
    for tariff in TARIFFS:
        if tariff.code == code:
            return tariff
    raise NotFound()
