"""The dashboard has NO LangGraph graph.

Unlike the LLM agents in the agents, the dashboard's work is HTTP fan-out across
the other agents (see ``aggregator.py``) rendered as server-side HTML. The real
HTTP entrypoint is ``api.py``.

This module hosts the dashboard's pure, deterministic *chart-shaping* helpers —
the only "compute" the dashboard owns (it owns no domain data). Keeping them here
(not in ``views.py``) keeps them framework-free and trivially unit-testable.
"""

from __future__ import annotations

from typing import Any

# Number of categorical series colors (--chart-1..--chart-6 in tokens.css). The
# series color index cycles through this set when series exceed it.
_CHART_COLORS = 6


def build_cost_chart(rows: list[dict[str, Any]], *, dim: str = "model") -> dict[str, Any]:
    """Pivot per-(day, series) cost rows into a stacked-bar structure: one bar per
    day, ``dim`` segments sized as a percent of the tallest day's total, plus a
    legend ordered by total spend. Pure/deterministic for easy testing.

    ``dim`` names the row field that labels each stacked series — ``"model"`` for
    the per-model chart, ``"agent"`` for the per-agent chart. Rows missing ``dim``
    fall back to a ``"model"`` field so the per-agent rollup (which carries both an
    ``agent`` key and a legacy ``model`` mirror) renders identically either way.
    The output ``legend``/segment entries are always keyed ``model`` so the
    rendering template stays uniform across both charts.
    """
    days_map: dict[Any, dict[str, float]] = {}
    series_total: dict[str, float] = {}
    for r in rows:
        day = r["day"]
        series = str(r.get(dim, r.get("model")))
        usd = float(r["usd"] or 0.0)
        days_map.setdefault(day, {})[series] = days_map.get(day, {}).get(series, 0.0) + usd
        series_total[series] = series_total.get(series, 0.0) + usd
    series_sorted = sorted(series_total, key=lambda m: (-series_total[m], m))
    color_of = {m: i % _CHART_COLORS for i, m in enumerate(series_sorted)}
    max_total = max((sum(segs.values()) for segs in days_map.values()), default=0.0)
    days: list[dict[str, Any]] = []
    for day in sorted(days_map):
        segs = days_map[day]
        total = sum(segs.values())
        segments = [
            {
                "model": m,
                "usd": round(segs[m], 6),
                "color_idx": color_of[m],
                "pct": round(segs[m] / total * 100, 2) if total else 0.0,
            }
            for m in sorted(segs, key=lambda m: (-segs[m], m))
        ]
        days.append(
            {
                "label": day.strftime("%m-%d") if hasattr(day, "strftime") else str(day),
                "total": round(total, 6),
                "height_pct": round(total / max_total * 100, 1) if max_total else 0.0,
                "segments": segments,
            }
        )
    legend = [
        {"model": m, "color_idx": color_of[m], "total": round(series_total[m], 6)}
        for m in series_sorted
    ]
    return {"days": days, "legend": legend, "total": round(sum(series_total.values()), 6)}
