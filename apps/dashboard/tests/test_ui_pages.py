"""@spec US-007, US-013 — UI pages: nav, node health, workspace, activity, audit export.

Tests for the UI pages suite: nav wiring, node health, workspace
members, activity stream + audit export."""

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


# ---- wire-1-2: nav + node health + workspace switcher ---------------------


@pytest.mark.asyncio
async def test_new_nav_links_present(client, admin_token) -> None:
    await login(client, admin_token)
    body = (await client.get("/")).text
    for href in ("/agents", "/workspace", "/activity", "/tokens", "/privacy"):
        assert f'href="{href}"' in body


@pytest.mark.asyncio
async def test_workspace_switcher_shows_current_tenant(client, admin_token) -> None:
    await login(client, admin_token)
    body = (await client.get("/")).text
    assert "ws-switch" in body
    assert "platform" in body  # principal.tenant from the admin_token fixture


@pytest.mark.asyncio
async def test_agents_page_renders_shell(client, admin_token) -> None:
    await login(client, admin_token)
    r = await client.get("/agents")
    assert r.status_code == 200
    assert "/panels/node-health" in r.text


@pytest.mark.asyncio
async def test_node_health_partial_requires_auth(client) -> None:
    # auth flip: the polled fragment now 401s (was a page-redirect before).
    assert (await client.get("/panels/node-health")).status_code == 401


@pytest.mark.asyncio
async def test_node_health_partial_renders_tiles(client, admin_token) -> None:
    await login(client, admin_token)
    r = await client.get("/panels/node-health")
    assert r.status_code == 200
    assert "cluster node health" in r.text
    # M1 fix: real status labels, never the constant fake "heartbeat: never".
    assert "heartbeat: never" not in r.text
    assert "healthy" in r.text  # green agents in GREEN_STATE


@pytest.mark.asyncio
async def test_nonadmin_token_redirects_to_login(client, settings) -> None:
    # A valid but non-admin token must bounce the page layer to /login (L1 fix),
    # not serve a bare 403.
    import time

    import jwt

    viewer = jwt.encode(
        {"sub": "v", "tenant": "platform", "role": "viewer", "exp": int(time.time()) + 3600},
        settings.jwt_secret,
        algorithm="HS256",
    )
    client.cookies.set("agents_dash", viewer)
    r = await client.get("/agents", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ---- members --------------------------------------------------------------


def test_workspace_context_derives_members(registry) -> None:
    class _P:
        sub = "op"
        tenant = "platform"
        role = "admin"

    ctx = views.workspace_context(registry, principal=_P())
    names = {m["name"] for m in ctx["members"]}
    assert "op" in names  # the human
    assert any(m["kind"] in ("service", "self") for m in ctx["members"])  # agents
    assert ctx["unavailable"] is None


def test_workspace_context_empty_registry_unavailable() -> None:
    class _P:
        sub = "op"
        tenant = "platform"
        role = "admin"

    ctx = views.workspace_context([], principal=_P())
    assert ctx["unavailable"] is not None
    assert ctx["members"][0]["name"] == "op"  # human still present


@pytest.mark.asyncio
async def test_workspace_partial_scopes_to_tenant(client, admin_token) -> None:
    await login(client, admin_token)
    r = await client.get("/partials/workspace")
    assert r.status_code == 200
    assert "platform" in r.text


# ---- activity + audit export ----------------------------------------------


@pytest.mark.asyncio
async def test_activity_partial_unavailable_when_no_db(client, admin_token) -> None:
    await login(client, admin_token)
    r = await client.get("/partials/activity")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


@pytest.mark.asyncio
async def test_sweep_appends_audit_row(client, admin_token, monkeypatch) -> None:
    await login(client, admin_token)
    rows: list[dict] = []

    async def fake_append(pool, **kw):
        rows.append(kw)

    monkeypatch.setattr(api_mod.audit, "append", fake_append)
    await csrf_post(client, "/actions/sweep")
    assert any(r["action"] == "sweep" and r["tenant_id"] == "platform" for r in rows)


@pytest.mark.asyncio
async def test_audit_export_requires_auth(client) -> None:
    assert (await client.get("/audit/export")).status_code == 401


@pytest.mark.asyncio
async def test_audit_export_bad_format(client, admin_token) -> None:
    await login(client, admin_token)
    assert (await client.get("/audit/export?format=xml")).status_code == 400


@pytest.mark.asyncio
async def test_audit_export_db_unavailable(client, admin_token) -> None:
    await login(client, admin_token)
    assert (await client.get("/audit/export?format=json")).status_code == 503


@pytest.mark.asyncio
async def test_audit_export_json_scopes_tenant(client, admin_token, monkeypatch) -> None:
    await login(client, admin_token)
    seen: dict = {}

    async def fake_iter(pool, *, tenant_id, limit=50000):
        seen["tenant_id"] = tenant_id
        for i in range(2):
            yield {
                "id": i,
                "ts": None,
                "actor": "op",
                "action": "sweep",
                "resource": "incidents",
                "metadata": {},
            }

    monkeypatch.setattr(api_mod.store, "iter_audit_log", fake_iter)
    api_mod.app.state.pool = object()
    try:
        r = await client.get("/audit/export?format=json")
    finally:
        api_mod.app.state.pool = None
    assert r.status_code == 200
    assert seen["tenant_id"] == "platform"  # tenant from principal, never a param
    assert "platform" in r.headers["content-disposition"]
    import json as _json

    assert len(_json.loads(r.text)) == 2


@pytest.mark.asyncio
async def test_audit_export_csv_has_header(client, admin_token, monkeypatch) -> None:
    await login(client, admin_token)

    async def fake_iter(pool, *, tenant_id, limit=50000):
        if False:  # empty stream
            yield {}

    monkeypatch.setattr(api_mod.store, "iter_audit_log", fake_iter)
    api_mod.app.state.pool = object()
    try:
        r = await client.get("/audit/export?format=csv")
    finally:
        api_mod.app.state.pool = None
    assert r.status_code == 200
    assert "id,ts,actor,action,resource,metadata" in r.text
    assert "attachment" in r.headers["content-disposition"]
