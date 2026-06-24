"""@spec US-013, US-007 — end-to-end dashboard over the registry.

End-to-end app tests: auth gating, login flow, screen + partial rendering.

Runs the real FastAPI app over an in-process ASGI transport. The DB layer is
monkeypatched (no Postgres/Redis) and the aggregator is replaced with one backed
by a mock agents, so every test is fully offline.
"""

from __future__ import annotations

import contextlib

import dashboard.api as api_mod
import httpx
import pytest
from asgi_lifespan import LifespanManager

from .conftest import (
    CSRF_COOKIE as CSRF_COOKIE_NAME,
)
from .conftest import (
    FakeRedis,
    csrf_cookie,
    csrf_post,
    login,
    make_aggregator,
    seed_csrf,
)
from .test_aggregator import _agents_handler

GREEN_STATE = {
    "monitoring": {
        "healthz": True,
        "ready": True,
        "metrics": {"error_rate_5m": 0.0, "llm_cost_usd_today": 1.0},
    },
    "security": {"healthz": True, "ready": True, "metrics": {"error_rate_5m": 0.0}},
    "pm": {"down": True},
    "web-ext-pipeline": {"down": True},
    "dashboard": {"healthz": True, "ready": True, "metrics": {"error_rate_5m": 0.0}},
}


@pytest.fixture
async def client(monkeypatch, settings):
    # No real DB: pg_pool yields None, redis is a fake.
    @contextlib.asynccontextmanager
    async def fake_pool(_settings, **kw):
        yield None

    monkeypatch.setattr(api_mod.agentdb, "pg_pool", fake_pool)
    monkeypatch.setattr(api_mod.agentdb, "redis_client", lambda s: FakeRedis())

    # Skip the background snapshot loop (needs a real pool).
    async def _noop_loop(app, **kw):
        return None

    monkeypatch.setattr(api_mod, "_snapshot_loop", _noop_loop)

    app = api_mod.app
    async with LifespanManager(app):
        # Swap in a mocked-agents aggregator after startup.
        app.state.aggregator = make_aggregator(settings, _agents_handler(GREEN_STATE))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        await app.state.aggregator.aclose()


@pytest.mark.asyncio
async def test_healthz_open(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["agent"] == "dashboard"


@pytest.mark.asyncio
async def test_root_redirects_to_login_when_unauthenticated(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_partial_requires_auth(client):
    r = await client.get("/partials/overview")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_bad_token(client):
    csrf = await seed_csrf(client)
    r = await client.post(
        "/login",
        data={"token": "not-a-jwt", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "invalid" in r.text


@pytest.mark.asyncio
async def test_login_without_csrf_token_is_rejected(client, admin_token):
    # No CSRF cookie/field at all → the middleware rejects before auth (403).
    r = await client.post("/login", data={"token": admin_token}, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_invalid_csrf_cookie_is_reissued_no_permanent_lockout(client, admin_token):
    """A browser presenting an INVALID/foreign-signed CSRF cookie (e.g. after a
    jwt_secret rotation, or a corrupted/truncated cookie) must get a FRESH valid
    cookie on the response — not be locked out forever. Regression for the
    presence-only re-issue check that refused to overwrite a now-invalid cookie,
    403-ing every POST (incl. POST /login) until the user cleared cookies."""
    # Plant a structurally-plausible but invalid (foreign-signed) CSRF cookie.
    client.cookies.set(CSRF_COOKIE_NAME, "deadbeef.notavalidsignature")
    assert not api_mod._csrf_valid("deadbeef.notavalidsignature")

    # GET /login must hand back a fresh, VALID cookie despite the bad one present.
    r = await client.get("/login")
    assert r.status_code == 200
    reissued = next(
        (c for c in r.headers.get_list("set-cookie") if c.startswith(f"{CSRF_COOKIE_NAME}=")),
        None,
    )
    assert reissued is not None, "no CSRF cookie re-issued for an invalid presented cookie"
    new_token = reissued.split(f"{CSRF_COOKIE_NAME}=", 1)[1].split(";", 1)[0]
    assert api_mod._csrf_valid(new_token)

    # The httpx jar now holds the fresh token; a subsequent POST /login with the
    # matching field SUCCEEDS (303) — the lockout is gone.
    csrf = csrf_cookie(client)
    assert api_mod._csrf_valid(csrf)
    r2 = await client.post(
        "/login",
        data={"token": admin_token, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r2.status_code == 303, r2.text


@pytest.mark.asyncio
async def test_csrf_exempt_only_ops_endpoints(client):
    """Ops endpoints inherited from agentkit are CSRF-exempt; dashboard actions
    and login are not."""
    assert api_mod._csrf_exempt("/healthz")
    assert api_mod._csrf_exempt("/metrics.json")
    assert not api_mod._csrf_exempt("/actions/sweep")
    assert not api_mod._csrf_exempt("/login")


@pytest.mark.asyncio
async def test_login_sets_cookie_and_grants_access(client, admin_token):
    csrf = await seed_csrf(client)
    r = await client.post(
        "/login",
        data={"token": admin_token, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookies = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("agents_dash="))
    assert "httponly" in session_cookie.lower()
    # env=="dev" in tests → Secure auto-resolves OFF (plain-http local).
    assert "secure" not in session_cookie.lower()
    # The readable CSRF companion cookie is (re)issued, non-HttpOnly so JS reads it.
    csrf = next(c for c in cookies if c.startswith("agents_csrf="))
    assert "httponly" not in csrf.lower()

    # Cookie persisted on the client → overview page now renders.
    page = await client.get("/")
    assert page.status_code == 200
    assert "Agents" in page.text or "agents" in page.text


def test_session_cookie_secure_defaults_by_env() -> None:
    # The session cookie carries the raw admin JWT, so Secure must be the default
    # everywhere EXCEPT dev (plain-http local). An explicit bool overrides.
    from dashboard.settings import Settings

    assert Settings(jwt_secret="x", env="dev").effective_session_cookie_secure is False
    assert Settings(jwt_secret="x", env="staging").effective_session_cookie_secure is True
    assert Settings(jwt_secret="x", env="prod").effective_session_cookie_secure is True
    # explicit override wins over the env-derived rule
    prod_off = Settings(jwt_secret="x", env="prod", session_cookie_secure=False)
    assert prod_off.effective_session_cookie_secure is False
    dev_on = Settings(jwt_secret="x", env="dev", session_cookie_secure=True)
    assert dev_on.effective_session_cookie_secure is True


@pytest.mark.asyncio
async def test_overview_partial_renders_tiles(client, admin_token):
    await login(client, admin_token)
    r = await client.get("/partials/overview")
    assert r.status_code == 200
    assert "healthy" in r.text
    assert "monitoring" in r.text
    assert "web-ext-pipeline" in r.text  # batch tile present


@pytest.mark.asyncio
async def test_incidents_partial_shows_unavailable_gracefully(client, admin_token, settings):
    # Point the aggregator at a agents where monitoring is down.
    api_mod.app.state.aggregator = make_aggregator(
        settings, _agents_handler({"monitoring": {"down": True}})
    )
    await login(client, admin_token)
    r = await client.get("/partials/incidents")
    assert r.status_code == 200
    assert "unreachable" in r.text


@pytest.mark.asyncio
async def test_cost_partial_renders(client, admin_token):
    await login(client, admin_token)
    r = await client.get("/partials/cost")
    assert r.status_code == 200
    assert "Total today" in r.text
    assert "Spend over time" in r.text


@pytest.mark.asyncio
async def test_sweep_action_proxied(client, admin_token, settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sweep" and request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    api_mod.app.state.aggregator = make_aggregator(settings, handler)
    await login(client, admin_token)
    r = await csrf_post(client, "/actions/sweep")
    assert r.status_code == 200
    assert "triggered" in r.text


@pytest.mark.asyncio
async def test_webext_run_forwards_and_shows_run_id(client, admin_token, settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/run" and request.method == "POST":
            import json as _json

            seen["body"] = _json.loads(request.content)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"run_id": "r-123", "status": "started"})
        return httpx.Response(404)

    api_mod.app.state.aggregator = make_aggregator(settings, handler)
    await login(client, admin_token)
    r = await csrf_post(client, "/actions/webext/run")
    assert r.status_code == 200
    assert "started" in r.text
    assert "r-123" in r.text
    # Conservative bounded default forwarded, with a minted admin Bearer.
    assert seen["body"] == {"limit": 1}
    assert seen["auth"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_webext_run_handles_409_already_running(client, admin_token, settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/run" and request.method == "POST":
            return httpx.Response(409, json={"detail": "a run is in progress"})
        return httpx.Response(404)

    api_mod.app.state.aggregator = make_aggregator(settings, handler)
    await login(client, admin_token)
    r = await csrf_post(client, "/actions/webext/run")
    assert r.status_code == 200
    assert "already in progress" in r.text


@pytest.mark.asyncio
async def test_webext_run_handles_unreachable_control_server(client, admin_token, settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    api_mod.app.state.aggregator = make_aggregator(settings, handler)
    await login(client, admin_token)
    r = await csrf_post(client, "/actions/webext/run")
    assert r.status_code == 200
    assert "web-ext control offline" in r.text


@pytest.mark.asyncio
async def test_webext_run_requires_auth(client):
    # CSRF-valid (cookie seeded by csrf_post) but unauthenticated → still 401.
    r = await csrf_post(client, "/actions/webext/run")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_action_without_csrf_is_forbidden(client, admin_token):
    # Authenticated, but a POST missing the CSRF token is rejected with 403.
    await login(client, admin_token)
    r = await client.post("/actions/sweep")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_action_with_mismatched_csrf_is_forbidden(client, admin_token):
    # A well-signed but DIFFERENT token (not matching the cookie) is rejected.
    await login(client, admin_token)
    forged = api_mod.issue_csrf_token()
    r = await client.post(
        "/actions/sweep",
        data={"csrf_token": forged},
        headers={"X-CSRF-Token": forged},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_action_cross_origin_is_forbidden(client, admin_token):
    # A mismatched Origin header is rejected even with a valid double-submit token.
    await login(client, admin_token)
    r = await client.post(
        "/actions/sweep",
        data={"csrf_token": csrf_cookie(client)},
        headers={"X-CSRF-Token": csrf_cookie(client), "Origin": "http://evil.example"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_logout_clears_cookie(client, admin_token):
    await login(client, admin_token)
    r = await csrf_post(client, "/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
