"""Shared test fixtures. All offline — no real agents, DB, or network.

A ``JWT_SECRET`` is set so token mint/verify works deterministically; the
aggregator is driven by an in-memory fake httpx transport so no socket is opened.
"""

from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test-secret-please-ignore")
os.environ.setdefault("AGENT_NAME", "dashboard")
os.environ.setdefault("POSTGRES_DB", "dashboard")
os.environ.setdefault("REDIS_DB", "4")

import time

import httpx
import jwt
import pytest
from dashboard.aggregator import Aggregator
from dashboard.auth import UpstreamToken
from dashboard.registry import build_registry
from dashboard.settings import Settings


@pytest.fixture(autouse=True)
def _disable_rate_limiter() -> None:
    """The app's per-IP rate limiter (agentkit) lives on a module-level singleton
    app; its in-process token bucket would otherwise accumulate across the whole
    test session (every request keys on ``127.0.0.1``) and spuriously 429 later
    tests. Neutralize it (rate<=0 short-circuits ``allow`` to True) and clear the
    bucket before each test so HTTP-flow cases stay independent. The rate-limit
    behavior itself is agentkit's concern and is covered in agentkit's tests."""
    import dashboard.api as api_mod

    limiter = getattr(api_mod.app.state, "rate_limiter", None)
    if limiter is not None:
        if hasattr(limiter, "_rate"):
            limiter._rate = 0
        if hasattr(limiter, "_local"):
            limiter._local.clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(jwt_secret="test-secret-please-ignore")


@pytest.fixture
def registry(settings: Settings):
    return build_registry(settings)


@pytest.fixture
def admin_token(settings: Settings) -> str:
    return jwt.encode(
        {"sub": "op", "tenant": "platform", "role": "admin", "exp": int(time.time()) + 3600},
        settings.jwt_secret,
        algorithm="HS256",
    )


class FakeRedis:
    """Minimal async redis stub: get/set/aclose used by the aggregator cache."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value

    async def aclose(self):
        pass


def make_aggregator(settings: Settings, handler) -> Aggregator:
    """Build an Aggregator whose httpx client is backed by a MockTransport.

    ``handler(httpx.Request) -> httpx.Response`` simulates the agents.
    """
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=2.0)
    return Aggregator(settings, UpstreamToken(settings), client=client, redis=FakeRedis())


# ---- CSRF helpers for app tests -------------------------------------------
# State-changing POSTs are CSRF-protected via a signed double-submit token: a
# readable ``agents_csrf`` cookie that must be echoed back in the hidden
# ``csrf_token`` form field (plain forms) or the ``X-CSRF-Token`` header (HTMX).
# To keep request volume identical to the pre-CSRF tests (the app's per-IP rate
# limiter accumulates across the shared in-process bucket), the helpers seed the
# readable cookie DIRECTLY with a freshly minted, validly-signed token rather
# than spending an extra GET /login round-trip to obtain one.

CSRF_COOKIE = "agents_csrf"
CSRF_HEADER = "X-CSRF-Token"


def csrf_cookie(client: httpx.AsyncClient) -> str:
    """The signed CSRF token the client currently holds (most recent value).

    Reads the jar directly rather than ``cookies.get`` — the server rotates the
    cookie on login, which can leave more than one same-named entry in the jar
    and make ``get`` raise. Empty string when the cookie hasn't been seeded yet
    (call :func:`seed_csrf` / hit a GET first)."""
    held = [c.value for c in client.cookies.jar if c.name == CSRF_COOKIE and c.value]
    return held[-1] if held else ""


async def seed_csrf(client: httpx.AsyncClient) -> str:
    """Obtain a server-issued CSRF cookie (a host-only cookie httpx sends back
    correctly) by hitting GET /login, and return its value. The app's rate
    limiter is neutralized in tests (see ``_disable_rate_limiter``) so the extra
    request is free."""
    await client.get("/login")
    return csrf_cookie(client)


async def login(client: httpx.AsyncClient, token: str) -> None:
    """Authenticate the test client through the real CSRF-protected login flow:
    GET /login to seed the readable CSRF cookie, then POST with the field."""
    csrf = await seed_csrf(client)
    await client.post("/login", data={"token": token, "csrf_token": csrf})


async def csrf_post(
    client: httpx.AsyncClient, url: str, *, data: dict | None = None
) -> httpx.Response:
    """POST to a protected route echoing the held CSRF token in both the header
    (covers HTMX-style calls) and the form field (covers plain forms). Seeds the
    cookie via GET /login first when the client holds none, so the double-submit
    has a matching value even on unauthenticated paths (which then 401 on the
    auth gate, not 403 on CSRF)."""
    csrf = csrf_cookie(client) or await seed_csrf(client)
    body = dict(data or {})
    body.setdefault("csrf_token", csrf)
    return await client.post(url, data=body, headers={CSRF_HEADER: csrf})
