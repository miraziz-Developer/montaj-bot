from decimal import Decimal

from app.worker.costs import CostRow, summarize_costs


def row(status: str, cost: str, units: int = 1, trial: bool = False) -> CostRow:
    return CostRow(status, trial, units, Decimal(cost))


def test_empty_is_all_zero() -> None:
    s = summarize_costs([])
    assert (s.jobs, s.done, s.failed, s.units_charged) == (0, 0, 0, 0)
    assert s.total_cost_usd == 0 and s.avg_cost_per_done_usd == 0 and s.cost_per_unit_usd == 0


def test_failed_jobs_cost_money_but_not_units() -> None:
    s = summarize_costs([row("DONE", "0.10", units=2), row("DONE", "0.20", units=2), row("FAILED", "0.05")])
    assert (s.jobs, s.done, s.failed) == (3, 2, 1)
    assert s.total_cost_usd == Decimal("0.35")
    assert s.avg_cost_per_done_usd == Decimal("0.1500")
    assert s.units_charged == 4
    assert s.cost_per_unit_usd == Decimal("0.0750")


def test_trial_cost_is_separated() -> None:
    s = summarize_costs([row("DONE", "0.10", trial=True), row("DONE", "0.30")])
    assert s.trial_cost_usd == Decimal("0.10")
