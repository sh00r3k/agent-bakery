"""@spec BR-008 — in-process metrics surface feeds the error-spike rule.

Offline unit tests for the in-process metrics surface (no DB/LLM/network)."""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
from agentkit.metrics import MetricsRegistry, RollingCounter


def test_rolling_counter_evicts_outside_window(monkeypatch):
    rc = RollingCounter(window_s=300.0)
    fake = {"t": 1000.0}
    monkeypatch.setattr(rc, "_now", lambda: fake["t"])

    rc.incr()
    rc.incr(2)
    assert rc.count() == 3

    # 4 minutes later: still in the 5m window
    fake["t"] = 1240.0
    rc.incr()
    assert rc.count() == 4

    # jump past the window from the first three events (they were at t=1000)
    fake["t"] = 1301.0
    # first three (t=1000) now older than 300s -> evicted; the t=1240 one stays
    assert rc.count() == 1


def test_error_rate_zero_when_no_requests():
    m = MetricsRegistry("t")
    assert m.error_rate_5m() == 0.0
    # errors with no requests must not divide by zero
    m.record_error()
    assert m.error_rate_5m() == 0.0


def test_error_rate_ratio(monkeypatch):
    m = MetricsRegistry("t")
    now = {"t": 5000.0}
    monkeypatch.setattr(m._requests, "_now", lambda: now["t"])
    monkeypatch.setattr(m._errors, "_now", lambda: now["t"])

    for _ in range(10):
        m.record_request()
    for _ in range(2):
        m.record_error()
    assert m.error_rate_5m() == pytest.approx(0.2)
    assert m.requests_5m() == 10
    assert m.errors_5m() == 2


def test_error_rate_decays_as_window_slides(monkeypatch):
    m = MetricsRegistry("t")
    now = {"t": 0.0}
    monkeypatch.setattr(m._requests, "_now", lambda: now["t"])
    monkeypatch.setattr(m._errors, "_now", lambda: now["t"])

    m.record_request()
    m.record_error()
    assert m.error_rate_5m() == pytest.approx(1.0)
    # slide the window fully past both events
    now["t"] = 301.0
    assert m.error_rate_5m() == 0.0


def test_uptime_is_positive():
    m = MetricsRegistry("t")
    time.sleep(0.01)
    assert m.uptime_s() > 0.0


@dataclass
class _FakeUsage:
    cost_usd: float = 0.0


def test_llm_cost_reads_usage_object():
    m = MetricsRegistry("t")
    assert m.llm_cost_usd_today() == 0.0  # none registered
    u = _FakeUsage(cost_usd=0.0)
    m.set_llm_usage(u)
    assert m.llm_cost_usd_today() == 0.0
    u.cost_usd = 1.2345
    assert m.llm_cost_usd_today() == pytest.approx(1.2345)


def test_cost_by_model_today():
    from types import SimpleNamespace

    m = MetricsRegistry("t")
    assert m.cost_by_model_today() == {}  # none registered
    m.set_llm_usage(SimpleNamespace(cost_usd=0.0, by_model={"minimax-m3": 0.5, "gpt-5": 0.25}))
    assert m.cost_by_model_today() == {"minimax-m3": 0.5, "gpt-5": 0.25}


async def test_snapshot_includes_cost_by_model():
    m = MetricsRegistry("security")
    snap = await m.snapshot()
    assert snap["cost_by_model_today"] == {}  # no usage registered


async def test_snapshot_shape_and_custom_and_last_run():
    m = MetricsRegistry("security")
    m.register("sync_metric", lambda: 7)

    async def _aprovider():
        return "async-ok"

    m.register("async_metric", _aprovider)

    async def _last_run():
        return {"ts": "2026-06-13T04:00:00Z", "status": "ok"}

    m.set_last_run_provider(_last_run)

    snap = await m.snapshot()
    assert snap["agent"] == "security"
    assert set(snap) >= {
        "agent",
        "uptime_s",
        "error_rate_5m",
        "last_run",
        "llm_cost_usd_today",
        "custom",
    }
    assert snap["custom"] == {"sync_metric": 7, "async_metric": "async-ok"}
    assert snap["last_run"] == {"ts": "2026-06-13T04:00:00Z", "status": "ok"}
    assert isinstance(snap["uptime_s"], float)
    assert snap["error_rate_5m"] == 0.0


async def test_snapshot_last_run_nullable():
    m = MetricsRegistry("monitoring")
    snap = await m.snapshot()
    assert snap["last_run"] is None  # scheduled-agent field, nullable for servers
