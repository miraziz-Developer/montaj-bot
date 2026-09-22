from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.core.config import Settings


def estimate_cost_usd(
    *,
    llm_input_tokens: int,
    llm_output_tokens: int,
    stt_seconds: float,
    render_seconds: float,
    settings: Settings,
) -> Decimal:
    """ARCHITECTURE section 14: tokens * price + stt_hours * price + render_hours * VM price."""
    total = (
        llm_input_tokens / 1_000_000 * settings.price_gemini_in_per_m_usd
        + llm_output_tokens / 1_000_000 * settings.price_gemini_out_per_m_usd
        + stt_seconds / 3600 * settings.price_stt_per_hour_usd
        + render_seconds / 3600 * settings.price_vm_per_hour_usd
    )
    return Decimal(str(round(total, 4)))


@dataclass(frozen=True)
class CostRow:
    """The columns of one finished job that the cost report needs."""

    status: str
    is_trial: bool
    units_cost: int
    est_cost_usd: Decimal


@dataclass(frozen=True)
class CostSummary:
    jobs: int
    done: int
    failed: int
    total_cost_usd: Decimal
    avg_cost_per_done_usd: Decimal
    trial_cost_usd: Decimal
    units_charged: int
    cost_per_unit_usd: Decimal


def summarize_costs(rows: Iterable[CostRow]) -> CostSummary:
    """Pure aggregation for scripts/cost_report.py. Failed jobs count towards the total (we paid
    for them) but not towards the per-DONE average or the unit numbers (they were refunded)."""
    rows = list(rows)
    done = [r for r in rows if r.status == "DONE"]
    failed = [r for r in rows if r.status == "FAILED"]
    total = sum((r.est_cost_usd for r in rows), Decimal(0))
    done_cost = sum((r.est_cost_usd for r in done), Decimal(0))
    units = sum(r.units_cost for r in done)
    zero = Decimal(0)
    return CostSummary(
        jobs=len(rows),
        done=len(done),
        failed=len(failed),
        total_cost_usd=total,
        avg_cost_per_done_usd=(done_cost / len(done)).quantize(Decimal("0.0001")) if done else zero,
        trial_cost_usd=sum((r.est_cost_usd for r in rows if r.is_trial), zero),
        units_charged=units,
        cost_per_unit_usd=(done_cost / units).quantize(Decimal("0.0001")) if units else zero,
    )
