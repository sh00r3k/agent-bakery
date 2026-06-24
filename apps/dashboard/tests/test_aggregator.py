"""@spec US-013 — registry-driven per-agent aggregation, graceful unavailable.

Aggregator: health composition, graceful unavailability, cost rollup."""

from __future__ import annotations

import httpx
import pytest
from dashboard.aggregator import AlreadyRunning, Unavailable
from dashboard.registry import by_slug

from .conftest import make_aggregator


def _agents_handler(state: dict) -> callable:
    """Return a MockTransport handler simulating per-host responses from `state`.

    state maps hostname -> {"healthz":bool, "ready":bool|None, "metrics":dict|None,
    "down":bool}. A `down` host raises ConnectError (agent unreachable).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        cfg = state.get(host, {})
        if cfg.get("down"):
            raise httpx.ConnectError("refused", request=request)
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200 if cfg.get("healthz", True) else 503, json={"status": "ok"})
        if path == "/readyz":
            ready = cfg.get("ready", True)
            code = 200 if ready else 503
            return httpx.Response(
                code,
                json={
                    "ready": bool(ready),
                    "checks": cfg.get("checks", {"postgres": True, "redis": ready}),
                },
            )
        if path == "/metrics.json":
            m = cfg.get("metrics")
            if m is None:
                return httpx.Response(404)
            return httpx.Response(200, json=m)
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_health_green_agent(settings, registry):
    state = {
        "monitoring": {
            "healthz": True,
            "ready": True,
            "metrics": {
                "uptime_s": 99.0,
                "error_rate_5m": 0.0,
                "requests_5m": 10,
                "llm_cost_usd_today": 1.25,
            },
        },
    }
    agg = make_aggregator(settings, _agents_handler(state))
    spec = by_slug(registry, "monitoring")
    dto = await agg.health(spec)
    assert dto.live is True
    assert dto.ready is True
    assert dto.status == "green"
    assert dto.llm_cost_usd_today == 1.25
    await agg.aclose()


@pytest.mark.asyncio
async def test_health_amber_on_readyz_503(settings, registry):
    state = {"security": {"healthz": True, "ready": False, "metrics": {"error_rate_5m": 0.0}}}
    agg = make_aggregator(settings, _agents_handler(state))
    dto = await agg.health(by_slug(registry, "security"))
    assert dto.live is True
    assert dto.ready is False
    assert dto.status == "amber"
    await agg.aclose()


@pytest.mark.asyncio
async def test_health_amber_on_high_error_rate(settings, registry):
    state = {"security": {"healthz": True, "ready": True, "metrics": {"error_rate_5m": 0.5}}}
    agg = make_aggregator(settings, _agents_handler(state))
    dto = await agg.health(by_slug(registry, "security"))
    assert dto.status == "amber"
    await agg.aclose()


@pytest.mark.asyncio
async def test_health_red_when_down(settings, registry):
    state = {"security": {"down": True}}
    agg = make_aggregator(settings, _agents_handler(state))
    dto = await agg.health(by_slug(registry, "security"))
    assert dto.live is False
    assert dto.status == "red"
    assert dto.error is not None
    await agg.aclose()


@pytest.mark.asyncio
async def test_batch_agent_is_grey_and_reads_last_run(settings, registry):
    # web-ext-pipeline has no /healthz port; surfaces last_run via metrics if any.
    state = {"web-ext-pipeline": {"down": True}}
    agg = make_aggregator(settings, _agents_handler(state))
    dto = await agg.health(by_slug(registry, "web-ext-pipeline"))
    assert dto.kind == "batch"
    assert dto.status == "grey"
    await agg.aclose()


@pytest.mark.asyncio
async def test_unavailable_marker_on_404(settings, registry):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    agg = make_aggregator(settings, handler)
    res = await agg.incidents(by_slug(registry, "monitoring"))
    assert isinstance(res, Unavailable)
    assert res.reason == "http 404"
    await agg.aclose()


@pytest.mark.asyncio
async def test_cost_rollup_sums_and_lists_unavailable(settings, registry):
    state = {
        "ultraqa": {"metrics": {"llm_cost_usd_today": 2.0}},
        "monitoring": {"metrics": {"llm_cost_usd_today": 1.0}},
        "security": {"down": True},
        "pm": {"metrics": {"llm_cost_usd_today": 0.5}},
        "web-ext-pipeline": {"metrics": {"llm_cost_usd_today": 0.25}},
    }
    agg = make_aggregator(settings, _agents_handler(state))
    rollup = await agg.cost_rollup(registry)
    assert rollup["total_usd_today"] == pytest.approx(3.75)
    assert rollup["per_agent"]["ultraqa"] == 2.0
    assert "security" in rollup["unavailable"]
    await agg.aclose()


@pytest.mark.asyncio
async def test_webext_runs_normalizes_array_shape(settings, registry):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/runs":
            return httpx.Response(
                200,
                json=[
                    {
                        "run_id": "r-1",
                        "status": "done",
                        "domains": ["a.com"],
                        "count": 1,
                        "cost_usd": 0.02,
                        "started_at": "t0",
                        "ended_at": "t1",
                    },
                ],
            )
        return httpx.Response(404)

    agg = make_aggregator(settings, handler)
    res = await agg.webext_runs(by_slug(registry, "web-ext-pipeline"))
    assert res["runs"][0]["run_id"] == "r-1"
    await agg.aclose()


@pytest.mark.asyncio
async def test_webext_run_returns_already_running_on_409(settings, registry):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/run":
            return httpx.Response(409, json={"detail": "busy"})
        return httpx.Response(404)

    agg = make_aggregator(settings, handler)
    res = await agg.webext_run(by_slug(registry, "web-ext-pipeline"))
    assert isinstance(res, AlreadyRunning)
    await agg.aclose()


@pytest.mark.asyncio
async def test_health_probes_concurrently(settings, registry):
    """health() must fire /healthz + /readyz + /metrics.json concurrently, not
    sequentially — one round-trip's latency for the tile, not three."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"ready": True, "checks": {"postgres": True}})
        if request.url.path == "/metrics.json":
            return httpx.Response(200, json={"uptime_s": 9.0, "llm_cost_usd_today": 1.0})
        return httpx.Response(404)

    agg = make_aggregator(settings, handler)
    dto = await agg.health(by_slug(registry, "monitoring"))
    assert dto.status == "green"
    assert dto.ready is True
    assert dto.llm_cost_usd_today == 1.0
    # all three endpoints were hit for a live agent (in one gather)
    assert set(seen) == {"/healthz", "/readyz", "/metrics.json"}
    await agg.aclose()


@pytest.mark.asyncio
async def test_health_down_agent_skips_ready_metrics_effect(settings, registry):
    """A non-batch agent whose /healthz is down is red; the concurrent /readyz +
    /metrics results are discarded (no stale ready/cost leaks onto a dead tile)."""
    state = {"monitoring": {"healthz": False, "ready": True, "metrics": {"llm_cost_usd_today": 9}}}
    agg = make_aggregator(settings, _agents_handler(state))
    dto = await agg.health(by_slug(registry, "monitoring"))
    assert dto.live is False
    assert dto.status == "red"
    assert dto.ready is None
    assert dto.llm_cost_usd_today is None
    await agg.aclose()


@pytest.mark.asyncio
async def test_batch_health_only_calls_metrics(settings, registry):
    """A batch agent must hit ONLY /metrics.json (no /healthz|/readyz — it has no
    port), reading its last_run/cost freshness from there."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/metrics.json":
            return httpx.Response(200, json={"last_run": {"id": "r1"}, "llm_cost_usd_today": 0.3})
        return httpx.Response(404)

    agg = make_aggregator(settings, handler)
    dto = await agg.health(by_slug(registry, "web-ext-pipeline"))
    assert dto.kind == "batch"
    assert dto.status == "grey"
    assert dto.last_run == {"id": "r1"}
    assert seen == ["/metrics.json"]
    await agg.aclose()


@pytest.mark.asyncio
async def test_agents_health_serves_cache_until_refresh(settings, registry):
    """agents_health serves the warm cache; refresh=True skips the cache READ
    (cold fan-out) yet still WRITES the fresh result back so the UI cache stays
    coherent with what a snapshot just recorded."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            calls["n"] += 1
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"ready": True, "checks": {}})
        if request.url.path == "/metrics.json":
            return httpx.Response(200, json={"uptime_s": 1.0})
        return httpx.Response(404)

    agg = make_aggregator(settings, handler)
    first = await agg.agents_health(registry)
    after_first = calls["n"]
    assert after_first > 0
    # second call served from cache: no new upstream healthz probes
    await agg.agents_health(registry)
    assert calls["n"] == after_first
    # refresh=True bypasses the cache read -> fresh fan-out (more probes)
    refreshed = await agg.agents_health(registry, refresh=True)
    assert calls["n"] > after_first
    assert {d.slug for d in refreshed} == {d.slug for d in first}
    # and it re-warmed the cache: a subsequent plain read hits cache again
    n_after_refresh = calls["n"]
    await agg.agents_health(registry)
    assert calls["n"] == n_after_refresh
    await agg.aclose()
