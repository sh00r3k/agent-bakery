"""@spec US-013 — view builders compose aggregator results into template context.

View builders compose aggregator results into template context dicts."""

from __future__ import annotations

import httpx
import pytest
from dashboard import views

from .conftest import make_aggregator
from .test_aggregator import _agents_handler


@pytest.mark.asyncio
async def test_overview_context_summarizes_agents(settings, registry):
    state = {
        "monitoring": {
            "healthz": True,
            "ready": True,
            "metrics": {"error_rate_5m": 0.0, "llm_cost_usd_today": 1.0},
        },
        "security": {"healthz": True, "ready": False, "metrics": {"error_rate_5m": 0.0}},
        "pm": {"down": True},
        "web-ext-pipeline": {"down": True},
        "ultraqa": {
            "healthz": True,
            "ready": True,
            "metrics": {"error_rate_5m": 0.0, "llm_cost_usd_today": 0.5},
        },
        "dashboard": {"healthz": True, "ready": True, "metrics": {"error_rate_5m": 0.0}},
    }
    agg = make_aggregator(settings, _agents_handler(state))
    ctx = await views.overview_context(agg, registry)
    s = ctx["summary"]
    # monitoring + ultraqa + dashboard green; security amber; pm red.
    assert s["healthy"] == 3
    assert s["degraded"] == 1
    assert s["down"] == 1
    assert s["llm_spend_today"] == pytest.approx(1.5)
    await agg.aclose()


@pytest.mark.asyncio
async def test_incidents_context_unavailable(settings, registry):
    state = {"monitoring": {"down": True}}
    agg = make_aggregator(settings, _agents_handler(state))
    ctx = await views.incidents_context(agg, registry)
    assert ctx["unavailable"] is not None
    assert ctx["incidents"] == []
    await agg.aclose()


@pytest.mark.asyncio
async def test_incidents_context_ok(settings, registry):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/incidents"):
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "incidents": [
                        {
                            "id": "i1",
                            "severity": "critical",
                            "title": "5xx spike",
                            "source": "sentry",
                            "count": 12,
                            "status": "open",
                            "last_seen": "now",
                        }
                    ],
                },
            )
        return httpx.Response(404)

    agg = make_aggregator(settings, handler)
    ctx = await views.incidents_context(agg, registry)
    assert ctx["unavailable"] is None
    assert ctx["incidents"][0]["severity"] == "critical"
    await agg.aclose()


@pytest.mark.asyncio
async def test_pm_context_unavailable_when_no_pm(settings, registry):
    state = {"pm": {"down": True}}
    agg = make_aggregator(settings, _agents_handler(state))
    ctx = await views.pm_context(agg, registry)
    assert ctx["unavailable"] is not None
    assert ctx["digests"] == [] and ctx["action_items"] == []
    await agg.aclose()


@pytest.mark.asyncio
async def test_cost_context(settings, registry):
    state = {"monitoring": {"metrics": {"llm_cost_usd_today": 3.0}}}
    agg = make_aggregator(settings, _agents_handler(state))
    ctx = await views.cost_context(agg, registry)
    assert ctx["rollup"]["per_agent"]["monitoring"] == 3.0
    # No pool → empty history, zero totals (degrades gracefully).
    assert ctx["windows"] == []
    assert ctx["totals"] == {"week": 0, "month": 0, "all_time": 0}
    await agg.aclose()


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        return _FakeCursor(self._rows)


class _FakePool:
    """Minimal pool that returns canned rows for the single windows query."""

    def __init__(self, rows):
        self._rows = rows

    def connection(self):
        return _FakeConn(self._rows)


@pytest.mark.asyncio
async def test_cost_context_windows_rollup(settings, registry):
    from datetime import datetime

    state = {"security": {"metrics": {"llm_cost_usd_today": 3.0}}}
    agg = make_aggregator(settings, _agents_handler(state))
    day = datetime(2026, 6, 19)
    pool = _FakePool(
        [
            ("security", 1.5, 4.0, 12.0, day),
            ("monitoring", 0.5, 0.5, 0.5, day),
        ]
    )
    ctx = await views.cost_context(agg, registry, pool=pool)
    assert [w["agent"] for w in ctx["windows"]] == ["security", "monitoring"]
    assert ctx["windows"][0]["all_time"] == 12.0
    assert ctx["windows"][0]["last_active"] == day
    assert ctx["totals"] == {"week": 2.0, "month": 4.5, "all_time": 12.5}
    await agg.aclose()


def test_build_cost_chart_pivots_days_and_models():
    from datetime import datetime

    d1, d2 = datetime(2026, 6, 18), datetime(2026, 6, 19)
    rows = [
        {"day": d1, "model": "minimax-m3", "usd": 1.0},
        {"day": d1, "model": "gpt-5", "usd": 3.0},  # d1 total 4.0 (tallest)
        {"day": d2, "model": "minimax-m3", "usd": 2.0},  # d2 total 2.0
    ]
    chart = views.build_cost_chart(rows)
    # legend ordered by total spend desc: gpt-5 (3.0) before minimax-m3 (3.0)? tie -> name
    assert [m["model"] for m in chart["legend"]] == ["gpt-5", "minimax-m3"]
    assert chart["total"] == pytest.approx(6.0)
    # color index is stable per model
    color = {m["model"]: m["color_idx"] for m in chart["legend"]}
    assert color == {"gpt-5": 0, "minimax-m3": 1}
    # days sorted ascending; tallest day scaled to 100%
    assert [d["label"] for d in chart["days"]] == ["06-18", "06-19"]
    assert chart["days"][0]["height_pct"] == 100.0
    assert chart["days"][1]["height_pct"] == 50.0
    # within d1, gpt-5 is 75% of the stack
    d1_segs = {s["model"]: s["pct"] for s in chart["days"][0]["segments"]}
    assert d1_segs["gpt-5"] == pytest.approx(75.0)
    assert d1_segs["minimax-m3"] == pytest.approx(25.0)


def test_build_cost_chart_empty():
    chart = views.build_cost_chart([])
    assert chart == {"days": [], "legend": [], "total": 0.0}


@pytest.mark.asyncio
async def test_overview_context_includes_cost_chart(settings, registry):
    from datetime import datetime

    agg = make_aggregator(settings, _agents_handler({}))
    day = datetime(2026, 6, 19)
    pool = _FakePool([(day, "gpt-5", 2.0), (day, "minimax-m3", 1.0)])
    ctx = await views.overview_context(agg, registry, pool=pool)
    assert ctx["cost_chart"]["total"] == pytest.approx(3.0)
    assert [m["model"] for m in ctx["cost_chart"]["legend"]] == ["gpt-5", "minimax-m3"]
    # The per-agent chart is built from the same pool (cost_agent_daily) and is
    # wired into the context for the second stacked bar on the overview.
    assert ctx["cost_chart_agent"]["total"] == pytest.approx(3.0)
    await agg.aclose()
