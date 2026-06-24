"""@spec US-013 — Personal Access Token crypto + dashboard routes.

Tests for Personal Access Tokens — crypto (pure) + dashboard routes."""

from __future__ import annotations

import contextlib

import dashboard.api as api_mod
import httpx
import pytest
from asgi_lifespan import LifespanManager
from dashboard import pat

from .conftest import FakeRedis, csrf_post, login, make_aggregator
from .test_aggregator import _agents_handler
from .test_app import GREEN_STATE

# ---- pure crypto ----------------------------------------------------------


def test_mint_pat_shape() -> None:
    secret, prefix, token_hash = pat.mint_pat()
    assert secret.startswith("ab_")
    assert prefix.startswith("ab_")
    assert len(token_hash) == 64  # sha256 hex
    assert secret != token_hash


def test_hash_is_sha256_of_full_secret() -> None:
    from hashlib import sha256

    secret = pat.new_pat_secret()
    assert pat.hash_pat(secret) == sha256(secret.encode()).hexdigest()


def test_verify_pat_roundtrip() -> None:
    secret, _prefix, token_hash = pat.mint_pat()
    assert pat.verify_pat(secret, stored_hash=token_hash) is True
    assert pat.verify_pat(secret + "x", stored_hash=token_hash) is False


def test_split_prefix_stable() -> None:
    secret = "ab_" + "A" * 43
    assert pat.split_prefix(secret) == "ab_" + "A" * 8


def test_prefix_derives_from_secret() -> None:
    secret, prefix, _ = pat.mint_pat()
    assert pat.split_prefix(secret) == prefix


# ---- app routes -----------------------------------------------------------


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


@pytest.mark.asyncio
async def test_tokens_page_redirects_when_unauthenticated(client) -> None:
    r = await client.get("/tokens", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_tokens_partial_requires_auth(client) -> None:
    r = await client.get("/partials/tokens")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tokens_partial_unavailable_when_no_db(client, admin_token) -> None:
    await login(client, admin_token)
    r = await client.get("/partials/tokens")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


@pytest.mark.asyncio
async def test_mint_reveals_secret_once_and_stores_hash(client, admin_token, monkeypatch) -> None:
    await login(client, admin_token)
    captured: dict = {}

    async def fake_create_pat(pool, **kw):
        captured.update(kw)
        return 1

    monkeypatch.setattr(api_mod.store, "create_pat", fake_create_pat)
    api_mod.app.state.pool = object()  # truthy → store path is exercised
    try:
        r = await csrf_post(
            client,
            "/actions/token/mint",
            data={"name": "ci", "scope": "read:agents", "role": "viewer", "expires_days": "30"},
        )
    finally:
        api_mod.app.state.pool = None
    assert r.status_code == 200
    assert "shown ONCE" in r.text
    # The stored row carries a hash, not the secret, and the principal's tenant.
    assert captured["tenant_id"] == "platform"
    assert captured["token_hash"] != ""
    assert "ab_" not in captured["token_hash"]  # hash, not the plaintext token


@pytest.mark.asyncio
async def test_mint_clamps_role_to_minter(client, admin_token, monkeypatch) -> None:
    # An admin minting a viewer token keeps viewer; the clamp never escalates.
    await login(client, admin_token)
    captured: dict = {}

    async def fake_create_pat(pool, **kw):
        captured.update(kw)
        return 1

    monkeypatch.setattr(api_mod.store, "create_pat", fake_create_pat)
    api_mod.app.state.pool = object()
    try:
        await csrf_post(
            client,
            "/actions/token/mint",
            data={"name": "ci", "role": "viewer", "expires_days": "9999"},
        )
    finally:
        api_mod.app.state.pool = None
    assert captured["role"] == "viewer"


@pytest.mark.asyncio
async def test_mint_requires_auth(client) -> None:
    # CSRF-valid (cookie seeded by csrf_post) but unauthenticated → still 401.
    r = await csrf_post(client, "/actions/token/mint", data={"name": "x"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mint_without_csrf_is_forbidden(client, admin_token) -> None:
    # Authenticated mint missing the CSRF token is rejected with 403.
    await login(client, admin_token)
    api_mod.app.state.pool = object()
    try:
        r = await client.post("/actions/token/mint", data={"name": "x"})
    finally:
        api_mod.app.state.pool = None
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_mint_appends_audit_row(client, admin_token, monkeypatch) -> None:
    await login(client, admin_token)

    async def fake_create_pat(pool, **kw):
        return 1

    rows: list[dict] = []

    async def fake_append(pool, **kw):
        rows.append(kw)

    monkeypatch.setattr(api_mod.store, "create_pat", fake_create_pat)
    monkeypatch.setattr(api_mod.audit, "append", fake_append)
    api_mod.app.state.pool = object()
    try:
        await csrf_post(client, "/actions/token/mint", data={"name": "ci", "role": "viewer"})
    finally:
        api_mod.app.state.pool = None
    assert any(r["action"] == "token-mint" and r["tenant_id"] == "platform" for r in rows)
