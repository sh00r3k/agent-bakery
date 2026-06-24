"""@spec US-012 — create_app exposes /healthz, /readyz, /metrics.json.

create_app wiring: /metrics.json contract, middleware counting, admin gate.

Uses FastAPI's TestClient (offline, in-process). No DB/LLM/network: /readyz is
not exercised so the DB ping is never called.
"""

from __future__ import annotations

import time

import jwt
import pytest
from agentkit.config import BaseAgentSettings
from agentkit.server import create_app
from fastapi import HTTPException
from fastapi.testclient import TestClient


def _make_token(secret: str, role: str = "admin", aud: str | None = None) -> str:
    claims = {"sub": "op", "tenant": "acme", "role": role, "exp": int(time.time()) + 300}
    if aud:
        claims["aud"] = aud
    return jwt.encode(claims, secret, algorithm="HS256")


def test_metrics_open_when_no_secret_and_counts_requests():
    settings = BaseAgentSettings(agent_name="testagent", jwt_secret="")
    app = create_app(settings, title="testagent")

    @app.get("/boom")
    def boom():
        raise HTTPException(status_code=500, detail="kaboom")

    @app.get("/ok")
    def ok():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)

    # baseline: healthz/metrics are uncounted
    r = client.get("/metrics.json")
    assert r.status_code == 200
    body = r.json()
    assert body["agent"] == "testagent"
    assert set(body) >= {
        "agent",
        "uptime_s",
        "error_rate_5m",
        "last_run",
        "llm_cost_usd_today",
        "custom",
    }
    assert body["last_run"] is None
    assert body["error_rate_5m"] == 0.0

    # 3 app requests, 1 of them a 500 -> error_rate 1/3
    client.get("/ok")
    client.get("/ok")
    client.get("/boom")
    body = client.get("/metrics.json").json()
    assert body["requests_5m"] == 3
    assert body["errors_5m"] == 1
    assert round(body["error_rate_5m"], 3) == round(1 / 3, 3)


def test_unhandled_exception_counts_as_error():
    settings = BaseAgentSettings(agent_name="t", jwt_secret="")
    app = create_app(settings)

    @app.get("/explode")
    def explode():
        raise RuntimeError("unhandled")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/explode")
    assert r.status_code == 500  # handled by agentkit exception handler
    body = client.get("/metrics.json").json()
    assert body["errors_5m"] == 1
    assert body["requests_5m"] == 1


def test_metrics_admin_gated_when_secret_set():
    secret = "s3cret"
    settings = BaseAgentSettings(agent_name="t", jwt_secret=secret)
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)

    # no token -> 401
    assert client.get("/metrics.json").status_code == 401
    # non-admin -> 403
    tok = _make_token(secret, role="viewer")
    assert (
        client.get("/metrics.json", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    )
    # admin -> 200
    tok = _make_token(secret, role="admin")
    r = client.get("/metrics.json", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_metrics_public_override_bypasses_gate():
    settings = BaseAgentSettings(agent_name="t", jwt_secret="s3cret")
    app = create_app(settings, metrics_public=True)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/metrics.json").status_code == 200


def test_registry_accessible_on_app_state():
    settings = BaseAgentSettings(agent_name="t", jwt_secret="")
    app = create_app(settings)

    app.state.metrics.register("backlog", lambda: 42)

    async def _last():
        return {"ts": "2026-06-13T04:00:00Z", "status": "ok"}

    app.state.metrics.set_last_run_provider(_last)

    client = TestClient(app, raise_server_exceptions=False)
    body = client.get("/metrics.json").json()
    assert body["custom"]["backlog"] == 42
    assert body["last_run"]["status"] == "ok"


# --- fail-closed JWT (AF-04) -------------------------------------------------


def test_fail_closed_when_secret_empty_and_env_not_dev():
    settings = BaseAgentSettings(agent_name="t", env="prod", jwt_secret="")
    with pytest.raises(RuntimeError, match="JWT_SECRET is empty"):
        create_app(settings)


def test_dev_still_allows_empty_secret():
    settings = BaseAgentSettings(agent_name="t", env="dev", jwt_secret="")
    app = create_app(settings)  # must not raise
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/metrics.json").status_code == 200


def test_metrics_public_override_allowed_without_secret_in_prod():
    # Explicit opt-in to public metrics is honoured even in prod (loud warning).
    settings = BaseAgentSettings(agent_name="t", env="prod", jwt_secret="")
    app = create_app(settings, metrics_public=True)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/metrics.json").status_code == 200


# --- prod docs gating (F7) ---------------------------------------------------


def test_docs_open_in_dev():
    app = create_app(BaseAgentSettings(agent_name="t", env="dev", jwt_secret="s3cret"))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_docs_gated_in_prod():
    app = create_app(BaseAgentSettings(agent_name="t", env="prod", jwt_secret="s3cret"))
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- request id correlation (F13) -------------------------------------------


def test_request_id_echoed_and_minted():
    app = create_app(BaseAgentSettings(agent_name="t", jwt_secret=""))

    @app.get("/ping")
    def ping():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    # minted when absent
    r = client.get("/ping")
    assert r.headers.get("X-Request-ID")
    # echoed back when supplied
    r = client.get("/ping", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


# --- CORS / TrustedHost allowlist (ARCH-009 / F17) ---------------------------


def test_cors_disabled_by_default():
    app = create_app(BaseAgentSettings(agent_name="t", jwt_secret=""))
    client = TestClient(app)

    @app.get("/x")
    def x():
        return {"ok": True}

    r = client.get("/x", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_allowlist_when_configured():
    app = create_app(
        BaseAgentSettings(agent_name="t", jwt_secret="", cors_allow_origins=["https://ops.example"])
    )

    @app.get("/x")
    def x():
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/x", headers={"Origin": "https://ops.example"})
    assert r.headers.get("access-control-allow-origin") == "https://ops.example"


def test_trusted_host_rejects_unknown_host():
    app = create_app(
        BaseAgentSettings(agent_name="t", jwt_secret="", trusted_hosts=["ops.example"])
    )
    client = TestClient(app)
    assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/healthz", headers={"Host": "ops.example"}).status_code == 200


# --- per-IP rate limit (ARCH-009) -------------------------------------------


def test_rate_limit_throttles_after_burst():
    # rate=2/min -> burst of 2, third request in the same instant is 429.
    app = create_app(BaseAgentSettings(agent_name="t", jwt_secret="", rate_limit_per_minute=2))

    @app.get("/hit")
    def hit():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/hit").status_code == 200
    assert client.get("/hit").status_code == 200
    r = client.get("/hit")
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "60"
    # ops paths stay exempt even under the limit
    assert client.get("/healthz").status_code == 200


def test_rate_limit_disabled_when_explicitly_zero():
    # 0 disables the limiter (the explicit opt-out).
    app = create_app(BaseAgentSettings(agent_name="t", jwt_secret="", rate_limit_per_minute=0))

    @app.get("/hit")
    def hit():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    for _ in range(10):
        assert client.get("/hit").status_code == 200


def test_rate_limit_on_by_default():
    # The default is now 60/min (on), not 0: a burst past the bucket is throttled
    # so an exposed agent is never accidentally unguarded.
    app = create_app(BaseAgentSettings(agent_name="t", jwt_secret=""))

    @app.get("/hit")
    def hit():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    codes = [client.get("/hit").status_code for _ in range(120)]
    assert 200 in codes
    assert 429 in codes  # burst of 60 exhausted -> later requests throttled


def test_rate_limit_keys_on_xff_when_trust_proxy():
    # With trust_proxy, the limiter keys on the RIGHTMOST X-Forwarded-For hop (the
    # one our single trusted proxy appended), so distinct immediate peers get
    # independent buckets even from one socket.
    app = create_app(
        BaseAgentSettings(agent_name="t", jwt_secret="", rate_limit_per_minute=2, trust_proxy=True)
    )

    @app.get("/hit")
    def hit():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    h_a = {"X-Forwarded-For": "10.0.0.1, 1.1.1.1"}
    h_b = {"X-Forwarded-For": "10.0.0.1, 2.2.2.2"}
    # client A burns its burst of 2 then gets throttled (keyed on rightmost 1.1.1.1)
    assert client.get("/hit", headers=h_a).status_code == 200
    assert client.get("/hit", headers=h_a).status_code == 200
    assert client.get("/hit", headers=h_a).status_code == 429
    # client B (different rightmost hop) has its own untouched bucket
    assert client.get("/hit", headers=h_b).status_code == 200


def test_rate_limit_forged_leftmost_xff_is_ignored_with_trust_proxy():
    # Our nginx APPENDS to XFF, so the leftmost value is client-controlled. An
    # attacker rotating only the leftmost entry cannot dodge the limit: keying is
    # on the rightmost (proxy-appended) hop, which stays constant here.
    app = create_app(
        BaseAgentSettings(agent_name="t", jwt_secret="", rate_limit_per_minute=2, trust_proxy=True)
    )

    @app.get("/hit")
    def hit():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    # Same trusted rightmost hop (3.3.3.3); attacker rotates the forgeable leftmost.
    assert client.get("/hit", headers={"X-Forwarded-For": "1.1.1.1, 3.3.3.3"}).status_code == 200
    assert client.get("/hit", headers={"X-Forwarded-For": "2.2.2.2, 3.3.3.3"}).status_code == 200
    # third request, yet another forged leftmost, still throttled (same bucket)
    assert client.get("/hit", headers={"X-Forwarded-For": "9.9.9.9, 3.3.3.3"}).status_code == 429


def test_rate_limit_unparseable_xff_falls_back_to_socket_peer():
    # A non-IP rightmost value (malformed/garbage) is ignored; keying falls back
    # to the socket peer so requests still share one bucket and get throttled.
    app = create_app(
        BaseAgentSettings(agent_name="t", jwt_secret="", rate_limit_per_minute=2, trust_proxy=True)
    )

    @app.get("/hit")
    def hit():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/hit", headers={"X-Forwarded-For": "not-an-ip"}).status_code == 200
    assert client.get("/hit", headers={"X-Forwarded-For": "still-garbage"}).status_code == 200
    assert client.get("/hit", headers={"X-Forwarded-For": "more-junk"}).status_code == 429


def test_rate_limit_ignores_xff_without_trust_proxy():
    # Without trust_proxy, a forged XFF is ignored: all requests share the socket
    # peer's bucket, so rotating the header cannot dodge the limit.
    app = create_app(
        BaseAgentSettings(agent_name="t", jwt_secret="", rate_limit_per_minute=2, trust_proxy=False)
    )

    @app.get("/hit")
    def hit():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/hit", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert client.get("/hit", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
    # third request, different forged XFF, still throttled (same socket bucket)
    assert client.get("/hit", headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 429
