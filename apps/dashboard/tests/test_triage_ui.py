"""@spec US-007, US-013 — triage actions + agent detail + activity filters.

v0.3 slice — triage actions, agent detail, activity filters.

View-level tests use a mocked-agents aggregator; route tests run the real app
over ASGI with pool=None (so the incident-triage overlay no-ops gracefully).
"""

from __future__ import annotations

import contextlib

import dashboard.api as api_mod
import httpx
import pytest
from asgi_lifespan import LifespanManager
from dashboard import views

from .conftest import FakeRedis, csrf_post, login, make_aggregator
from .test_aggregator import _agents_handler
from .test_app import GREEN_STATE


@pytest.fixture
async def client(monkeypatch, settings):
    """Offline app + client (mirrors test_app.client): no DB (pool=None), fake
    redis, mocked-agents aggregator, ASGI transport."""

    @contextlib.asynccontextmanager
    async def fake_pool(_settings, **kw):
        yield None

    monkeypatch.setattr(api_mod.agentdb, "pg_pool", fake_pool)
    monkeypatch.setattr(api_mod.agentdb, "redis_client", lambda s: FakeRedis())

    async def _noop_loop(app, **kw):
        return None

    monkeypatch.setattr(api_mod, "_snapshot_loop", _noop_loop)
    app = api_mod.app
    async with LifespanManager(app):
        app.state.aggregator = make_aggregator(settings, _agents_handler(GREEN_STATE))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        await app.state.aggregator.aclose()


# ---- views ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_detail_unknown_slug(settings, registry):
    agg = make_aggregator(settings, _agents_handler({}))
    ctx = await views.agent_detail_context(agg, registry, slug="nope", pool=None)
    assert ctx["unavailable"] is not None
    assert ctx["health"] is None
    await agg.aclose()


@pytest.mark.asyncio
async def test_agent_detail_known_slug(settings, registry):
    state = {"ultraqa": {"healthz": True, "ready": True, "metrics": {"error_rate_5m": 0.0}}}
    agg = make_aggregator(settings, _agents_handler(state))
    ctx = await views.agent_detail_context(agg, registry, slug="ultraqa", pool=None)
    assert ctx["unavailable"] is None
    assert ctx["health"].slug == "ultraqa"
    assert ctx["heartbeats"] == []  # pool=None → no time-series
    await agg.aclose()


@pytest.mark.asyncio
async def test_incidents_context_triage_empty_without_pool(settings, registry):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/incidents"):
            return httpx.Response(
                200,
                json={"incidents": [{"dedup_key": "d1", "severity": "critical", "title": "x"}]},
            )
        return httpx.Response(404)

    agg = make_aggregator(settings, handler)
    ctx = await views.incidents_context(agg, registry)
    assert ctx["triage"] == {}
    assert ctx["incidents"][0]["dedup_key"] == "d1"
    await agg.aclose()


@pytest.mark.asyncio
async def test_activity_context_carries_filter_state(settings):
    # pool=None → graceful "unavailable"; the selected filters are echoed back so
    # the partial can re-render its controls (the q/action/actor round-trip).
    ctx = await views.activity_context(None, tenant="platform", q="login", action="sweep")
    assert ctx["unavailable"] is not None
    assert ctx["events"] == [] and ctx["actions"] == []
    assert ctx["filter"]["q"] == "login" and ctx["filter"]["action"] == "sweep"


# ---- routes -----------------------------------------------------------------
async def _login(client: httpx.AsyncClient, admin_token: str) -> None:
    await login(client, admin_token)


@pytest.mark.asyncio
async def test_agent_detail_page_and_partial(client, admin_token):
    await _login(client, admin_token)
    r = await client.get("/agents/ultraqa")
    assert r.status_code == 200
    assert "ultraqa" in r.text
    p = await client.get("/partials/agent/ultraqa")
    assert p.status_code == 200


@pytest.mark.asyncio
async def test_incident_triage_validates_status(client, admin_token):
    await _login(client, admin_token)
    ok = await csrf_post(
        client, "/actions/incident/triage", data={"dedup_key": "d1", "status": "resolved"}
    )
    assert ok.status_code == 200  # pool=None → overlay no-ops, list re-renders
    bad = await csrf_post(
        client, "/actions/incident/triage", data={"dedup_key": "d1", "status": "bogus"}
    )
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_finding_resolve_validates_status(client, admin_token):
    await _login(client, admin_token)
    bad = await csrf_post(
        client, "/actions/finding/resolve", data={"dedup_key": "f1", "status": "bogus"}
    )
    assert bad.status_code == 400
    ok = await csrf_post(
        client,
        "/actions/finding/resolve",
        data={"dedup_key": "f1", "status": "dismissed", "source": "qa"},
    )
    assert ok.status_code == 200  # proxy attempt → re-renders the qa partial


@pytest.mark.asyncio
async def test_qa_scan_action(client, admin_token):
    await _login(client, admin_token)
    r = await csrf_post(client, "/actions/qa/scan")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_activity_partial_accepts_filters(client, admin_token):
    await _login(client, admin_token)
    r = await client.get("/partials/activity", params={"action": "login", "q": "op"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_activity_load_older_cursor_survives_q_filter(settings, monkeypatch):
    """Regression: the keyset cursor must come from the RAW DB page, not the
    post-q-filtered list — else q-filtered views can never page past page 1."""
    from datetime import UTC, datetime

    from dashboard import views as v

    rows = [
        {
            "id": i,
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "actor": "op",
            "action": "login" if i == 0 else "sweep",
            "resource": None,
            "metadata": {},
        }
        for i in range(100)
    ]

    async def fake_query(pool, **kw):
        return rows

    async def fake_distinct(pool, **kw):
        return ["login", "sweep"]

    monkeypatch.setattr(v.audit, "query", fake_query)
    monkeypatch.setattr(v.audit, "distinct_actions", fake_distinct)
    ctx = await v.activity_context(object(), tenant="platform", q="login", limit=100)
    assert len(ctx["events"]) == 1  # only one row matches q
    assert ctx["next_before"] is not None  # but the full DB page → cursor present


@pytest.mark.asyncio
async def test_triage_rejects_empty_dedup_key(client, admin_token):
    await _login(client, admin_token)
    inc = await csrf_post(
        client, "/actions/incident/triage", data={"dedup_key": "  ", "status": "resolved"}
    )
    assert inc.status_code == 400
    fnd = await csrf_post(
        client, "/actions/finding/resolve", data={"dedup_key": "   ", "status": "fixed"}
    )
    assert fnd.status_code == 400
