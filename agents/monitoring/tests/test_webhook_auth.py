"""@spec BR-010 — webhook ingress is HMAC-gated, oversized bodies 413.

Webhook ingress hardening tests (ARCH-001 / AF-01 / AF-08).

The two ``/webhook/*`` routes are HMAC-gated: an unsigned or wrong-signature
POST is rejected with 401, an oversized body with 413, and a correctly-signed
body is triaged. We mount the real route handlers from ``monitoring_agent.api``
on a fresh FastAPI app with a fake triage graph so no DB / RabbitMQ / LLM is
touched and the import-time lifespan never runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import monitoring_agent.api as api_mod
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeGraph:
    async def ainvoke(self, state, config=None):
        sig = state["signal"]
        return {"severity": sig.suggested_severity, "alerted": True, "category": "x"}


@pytest.fixture
def client(monkeypatch) -> TestClient:
    secret = "s3cr3t-test-key"
    monkeypatch.setattr(api_mod.settings, "webhook_secret", secret, raising=False)
    monkeypatch.setattr(api_mod.settings, "webhook_max_body_bytes", 1024, raising=False)
    monkeypatch.setattr(api_mod.settings, "webhook_max_alerts", 3, raising=False)

    app = FastAPI()
    app.state.graph = _FakeGraph()
    # The handlers reference the module-global ``app`` for ``app.state``; point
    # it at our throwaway app so they read the fake graph.
    monkeypatch.setattr(api_mod, "app", app, raising=False)
    app.add_api_route("/webhook/sentry", api_mod.webhook_sentry, methods=["POST"])
    app.add_api_route("/webhook/alert", api_mod.webhook_alert, methods=["POST"])
    c = TestClient(app)
    c._secret = secret  # type: ignore[attr-defined]
    return c


def _sign(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_sentry_unsigned_is_401(client: TestClient) -> None:
    r = client.post("/webhook/sentry", json={"message": "x"})
    assert r.status_code == 401


def test_sentry_bad_signature_is_401(client: TestClient) -> None:
    raw = json.dumps({"message": "x"}).encode()
    r = client.post(
        "/webhook/sentry",
        content=raw,
        headers={"Sentry-Hook-Signature": "deadbeef"},
    )
    assert r.status_code == 401


def test_sentry_valid_signature_triaged(client: TestClient) -> None:
    raw = json.dumps({"message": "boom", "level": "fatal"}).encode()
    sig = _sign(client._secret, raw)  # type: ignore[attr-defined]
    r = client.post(
        "/webhook/sentry",
        content=raw,
        headers={"Sentry-Hook-Signature": sig, "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["severity"] == "critical"  # fatal -> critical


def test_sentry_sha256_prefixed_signature_accepted(client: TestClient) -> None:
    raw = json.dumps({"message": "boom"}).encode()
    sig = "sha256=" + _sign(client._secret, raw)  # type: ignore[attr-defined]
    r = client.post(
        "/webhook/sentry",
        content=raw,
        headers={"Sentry-Hook-Signature": sig, "content-type": "application/json"},
    )
    assert r.status_code == 200


def test_oversized_body_is_413(client: TestClient) -> None:
    raw = json.dumps({"message": "x" * 5000}).encode()
    sig = _sign(client._secret, raw)  # type: ignore[attr-defined]
    r = client.post(
        "/webhook/sentry",
        content=raw,
        headers={"Sentry-Hook-Signature": sig, "content-type": "application/json"},
    )
    assert r.status_code == 413


def test_alert_route_uses_generic_header_and_caps_array(client: TestClient) -> None:
    alerts = {"alerts": [{"name": f"a{i}", "severity": "warning"} for i in range(10)]}
    raw = json.dumps(alerts).encode()
    sig = _sign(client._secret, raw)  # type: ignore[attr-defined]
    r = client.post(
        "/webhook/alert",
        content=raw,
        headers={"X-Webhook-Signature": sig, "content-type": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["received"] == 3  # capped at webhook_max_alerts
    assert body["dropped"] == 7


def test_alert_route_rejects_sentry_header(client: TestClient) -> None:
    raw = json.dumps({"name": "x"}).encode()
    sig = _sign(client._secret, raw)  # type: ignore[attr-defined]
    # Signature present but under the wrong header name -> unsigned for this route.
    r = client.post(
        "/webhook/alert",
        content=raw,
        headers={"Sentry-Hook-Signature": sig},
    )
    assert r.status_code == 401


def test_empty_secret_fails_closed(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(api_mod.settings, "webhook_secret", "", raising=False)
    raw = json.dumps({"message": "x"}).encode()
    sig = _sign("", raw)
    r = client.post(
        "/webhook/sentry",
        content=raw,
        headers={"Sentry-Hook-Signature": sig},
    )
    assert r.status_code == 401
