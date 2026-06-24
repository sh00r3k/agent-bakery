"""@spec US-013, BR-010 — privacy / local-first trust panel.

Tests for the privacy / local-first trust panel."""

from __future__ import annotations

import contextlib

import dashboard.api as api_mod
import httpx
import pytest
from asgi_lifespan import LifespanManager
from dashboard import views
from dashboard.settings import Settings

from .conftest import FakeRedis, login, make_aggregator
from .test_aggregator import _agents_handler
from .test_app import GREEN_STATE

# ---- pure context ---------------------------------------------------------


def test_privacy_context_off_by_default() -> None:
    ctx = views.privacy_context(
        Settings(jwt_secret="x", rabbitmq_url="amqp://user:pass@rabbitmq:5672/")
    )
    assert ctx["private_mode"] is False
    names = {a["name"] for a in ctx["allowed"]}
    assert {"LLM gateway", "Postgres", "Redis", "RabbitMQ"} <= names


def test_privacy_context_skips_blank_rabbitmq() -> None:
    ctx = views.privacy_context(Settings(jwt_secret="x", rabbitmq_url=""))
    names = {a["name"] for a in ctx["allowed"]}
    assert "RabbitMQ" not in names


def test_privacy_context_on_disables_observability() -> None:
    ctx = views.privacy_context(
        Settings(
            jwt_secret="x",
            private_mode=True,
            otel_exporter_otlp_endpoint="http://collector:4318",
        )
    )
    assert ctx["private_mode"] is True
    assert ctx["otel_enabled"] is False  # forced off under private mode


def test_privacy_context_skips_blank_url() -> None:
    ctx = views.privacy_context(Settings(jwt_secret="x", llm_base_url=""))
    names = {a["name"] for a in ctx["allowed"]}
    assert "LLM gateway" not in names  # unparseable URL dropped, not mislabeled


def test_privacy_context_never_leaks_raw_dsn() -> None:
    ctx = views.privacy_context(Settings(jwt_secret="x", postgres_password="s3cret"))
    for a in ctx["allowed"]:
        assert "s3cret" not in a["host"]
        assert set(a.keys()) == {"name", "host", "port"}


# ---- routes ---------------------------------------------------------------


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
async def test_privacy_partial_requires_auth(client) -> None:
    assert (await client.get("/partials/privacy")).status_code == 401


@pytest.mark.asyncio
async def test_privacy_partial_renders(client, admin_token) -> None:
    await login(client, admin_token)
    r = await client.get("/partials/privacy")
    assert r.status_code == 200
    assert "Private Mode" in r.text
    assert "egress allow-list" in r.text
