"""Print what the last N days of jobs cost (est_cost_usd) and what one unit costs us.

docker compose run --rm api python scripts/cost_report.py --days 7
docker compose run --rm api python scripts/cost_report.py --days 30 --unit-price-usd 0.5
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.db import get_engine, get_sessionmaker
from app.models import Job
from app.models.enums import JobStatus
from app.worker.costs import CostRow, summarize_costs


async def main(days: int, unit_price: Decimal | None) -> None:
    engine, sessionmaker = get_engine(), get_sessionmaker()
    since = datetime.now(UTC) - timedelta(days=days)
    try:
        async with sessionmaker() as session:
            result = await session.execute(
                select(Job.status, Job.is_trial, Job.units_cost, Job.est_cost_usd).where(
                    Job.created_at >= since, Job.status.in_([JobStatus.DONE, JobStatus.FAILED])
                )
            )
            rows = [CostRow(str(s), t, u, c) for s, t, u, c in result.all()]
    finally:
        await engine.dispose()
    s = summarize_costs(rows)
    print(f"Last {days} days: {s.jobs} finished jobs ({s.done} done, {s.failed} failed)")
    print(f"  total cost            : ${s.total_cost_usd}")
    print(f"  avg cost per DONE job : ${s.avg_cost_per_done_usd}")
    print(f"  trial jobs cost       : ${s.trial_cost_usd}")
    print(f"  units charged         : {s.units_charged}")
    print(f"  cost per unit         : ${s.cost_per_unit_usd}")
    if unit_price is not None and s.units_charged:
        margin = unit_price - s.cost_per_unit_usd
        print(f"  margin per unit       : ${margin} at ${unit_price}/unit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--unit-price-usd", type=Decimal, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.days, args.unit_price_usd))
