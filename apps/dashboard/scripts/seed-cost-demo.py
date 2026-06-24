#!/usr/bin/env python3
"""Seed tiny demo per-model cost rows into the dashboard's own DB — purely so the
overview's "LLM spend by model" and "LLM spend by agent" stacked bar charts have
something to render for a screenshot.

Both charts read the SAME table (``cost_model_events``: agent + model + ts +
usd_today), so one seed feeds both: :func:`store.cost_model_daily` sums across
agents per model, :func:`store.cost_agent_daily` sums across models per agent.

Amounts are deliberately MISERLY — single-digit cents — like a real dev contour
that has barely made any LLM calls. Daily-max semantics (the chart takes
``max(usd_today)`` per agent/model/day) means one cumulative snapshot row per
(agent, model, day) is enough.

Idempotent: it first deletes any rows it previously inserted for these demo
agents inside the window, then re-inserts, so re-running won't double-count.

Run against a reachable dashboard DB (honors the same POSTGRES_* / DASHBOARD_*
env as the app):

    cd apps/dashboard && python scripts/seed-cost-demo.py
    # or override the window:  DEMO_DAYS=14 python scripts/seed-cost-demo.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

from agentkit import db as agentdb
from dashboard import store
from dashboard.settings import get_settings

# (agent, {model: cents-per-day-step}). Cents grow a touch each day so bars
# differ in height; spread across agents/models so both stacks read distinctly.
_DEMO: dict[str, dict[str, float]] = {
    "monitoring": {"minimax-m3": 0.004, "gpt-5": 0.006},
    "security": {"claude-sonnet": 0.009, "gpt-5": 0.003},
    "example-agent": {"gpt-5": 0.007, "minimax-m3": 0.002},
    "pm": {"claude-sonnet": 0.005},
}
_DEMO_AGENTS = tuple(_DEMO)


async def main() -> None:
    days = int(os.environ.get("DEMO_DAYS", "14"))
    settings = get_settings()
    async with agentdb.pg_pool(settings) as pool:
        await store.create_schema(pool)
        # Idempotency: drop prior demo rows for these agents in the window so a
        # re-run replaces rather than stacks on top of itself.
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM cost_model_events "
                "WHERE agent = ANY(%s) AND ts > now() - make_interval(days => %s)",
                (list(_DEMO_AGENTS), days + 1),
            )
        midnight = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        rows = 0
        for d in range(days):
            ts = midnight - timedelta(days=days - 1 - d)
            # Day index scales spend slightly — a gentle ramp, still all cents.
            ramp = 1.0 + d * 0.15
            for agent, by_model in _DEMO.items():
                snapshot = {m: round(cents * ramp, 6) for m, cents in by_model.items()}
                await store.record_cost_by_model(pool, agent=agent, by_model=snapshot, ts=ts)
                rows += len(snapshot)
        print(f"seeded {rows} demo cost rows across {len(_DEMO)} agents over {days} days")


if __name__ == "__main__":
    asyncio.run(main())
