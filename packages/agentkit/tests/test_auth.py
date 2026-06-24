"""@spec BR-002 — JWT verification + webhook HMAC enforce tenant identity.

Direct tests for the auth crown jewel: JWT verification + webhook HMAC.

``agentkit.auth`` is the cross-agent authn primitive — every agent's metrics
gate, the dashboard session/upstream token, and the monitoring admin endpoints
route through ``verify_token``. These tests exercise it directly (offline, no
DB/network), covering the happy path, every failure branch, and the
security-critical forgery cases an attacker probes: ``alg:none``, HS-vs-RS
confusion, expired/missing-claim tokens, and the bearer-scheme parsing.
"""

from __future__ import annotations

import time

import jwt
import pytest
from agentkit.auth import (
    AuthError,
    Principal,
    make_principal_dependency,
    verify_token,
    verify_webhook_signature,
)

SECRET = "shared-secret"


def _token(secret: str = SECRET, *, alg: str = "HS256", **claims) -> str:
    base = {"sub": "op", "tenant": "acme", "role": "admin", "exp": int(time.time()) + 300}
    base.update(claims)
    # Allow callers to delete a default claim by passing it as None.
    base = {k: v for k, v in base.items() if v is not None}
    return jwt.encode(base, secret, algorithm=alg)


def _verify(token: str, *, secret: str = SECRET, audience: str | None = None) -> Principal:
    return verify_token(token, secret=secret, algorithms=["HS256"], audience=audience)


# --- happy path -------------------------------------------------------------
def test_valid_admin_token_yields_admin_principal():
    p = _verify(_token(role="admin"))
    assert isinstance(p, Principal)
    assert p.sub == "op"
    assert p.tenant == "acme"
    assert p.is_admin is True


def test_valid_manager_token_is_admin():
    assert _verify(_token(role="manager")).is_admin is True


def test_viewer_role_is_not_admin():
    p = _verify(_token(role="viewer"))
    assert p.role == "viewer"
    assert p.is_admin is False


def test_name_claim_is_passed_through():
    assert _verify(_token(name="Ada")).name == "Ada"


def test_audience_is_validated_when_expected():
    tok = _token(aud="agents")
    assert _verify(tok, audience="agents").tenant == "acme"
    with pytest.raises(AuthError):
        _verify(tok, audience="other")


def test_aud_claim_required_when_audience_configured():
    # When an audience is configured, a token WITHOUT an `aud` claim must be
    # rejected (require list adds 'aud'), not silently accepted.
    tok = _token()  # no aud claim
    with pytest.raises(AuthError):
        _verify(tok, audience="agents")


def test_aud_not_required_when_audience_unset():
    # Backward-compatible dev path: no configured audience -> a token WITHOUT an
    # `aud` claim verifies fine (the shared-secret setup mints no audience).
    tok = _token()  # no aud claim
    assert _verify(tok, audience=None).tenant == "acme"


# --- claim / config failure branches ---------------------------------------
def test_empty_secret_means_not_configured():
    with pytest.raises(AuthError) as exc:
        _verify(_token(), secret="")
    assert "auth not configured" in str(exc.value.detail)
    assert exc.value.status_code == 401


def test_expired_token_rejected():
    tok = _token(exp=int(time.time()) - 10)
    with pytest.raises(AuthError) as exc:
        _verify(tok)
    assert "expired" in str(exc.value.detail)


def test_missing_exp_rejected():
    # options={"require": ["exp", "sub"]} must reject a token with no exp.
    tok = _token(exp=None)
    with pytest.raises(AuthError):
        _verify(tok)


def test_missing_sub_rejected():
    tok = _token(sub=None)
    with pytest.raises(AuthError):
        _verify(tok)


def test_missing_tenant_and_no_iss_fallback_rejected():
    # AF-06: dropping the `iss` fallback — an `iss`-only token must NOT pass.
    tok = _token(tenant=None, iss="https://issuer.example")
    with pytest.raises(AuthError) as exc:
        _verify(tok)
    assert "missing tenant/role" in str(exc.value.detail)


def test_missing_role_rejected():
    tok = _token(role=None)
    with pytest.raises(AuthError) as exc:
        _verify(tok)
    assert "missing tenant/role" in str(exc.value.detail)


# --- security-critical forgery cases ---------------------------------------
def test_alg_none_token_is_rejected():
    # Unsigned `alg:none` token must never be accepted when HS256 is pinned.
    forged = jwt.encode(
        {"sub": "op", "tenant": "acme", "role": "admin", "exp": int(time.time()) + 300},
        "",
        algorithm="none",
    )
    with pytest.raises(AuthError):
        _verify(forged)


def test_hs_vs_rs_algorithm_confusion_is_rejected():
    # A token forged under a different family/secret must fail the HS256 pin.
    forged = jwt.encode(
        {"sub": "op", "tenant": "acme", "role": "admin", "exp": int(time.time()) + 300},
        "attacker-key",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        _verify(forged)  # verified against the real SECRET -> signature mismatch


def test_wrong_secret_is_rejected():
    with pytest.raises(AuthError):
        _verify(_token(secret="other-secret"))


def test_garbage_token_is_rejected():
    with pytest.raises(AuthError):
        _verify("not-a-jwt")


# --- bearer-scheme parsing via make_principal_dependency -------------------
def _settings():
    from agentkit.config import BaseAgentSettings

    return BaseAgentSettings(agent_name="t", jwt_secret=SECRET)


def test_dependency_accepts_valid_bearer():
    dep = make_principal_dependency(_settings())
    p = dep(authorization=f"Bearer {_token()}")
    assert p.is_admin is True


def test_dependency_missing_authorization_rejected():
    dep = make_principal_dependency(_settings())
    with pytest.raises(AuthError) as exc:
        dep(authorization="")
    assert "missing bearer token" in str(exc.value.detail)


def test_dependency_non_bearer_scheme_rejected():
    dep = make_principal_dependency(_settings())
    with pytest.raises(AuthError):
        dep(authorization=f"Basic {_token()}")


def test_dependency_bearer_but_empty_token_rejected():
    dep = make_principal_dependency(_settings())
    with pytest.raises(AuthError):
        dep(authorization="Bearer ")


# --- webhook HMAC helper ----------------------------------------------------
def test_verify_webhook_signature_accepts_matching_digest():
    import hashlib
    import hmac

    body = b'{"event":"alert"}'
    sig = hmac.new(b"wh-secret", body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, sig, "wh-secret") is True
    assert verify_webhook_signature(body, f"sha256={sig}", "wh-secret") is True


def test_verify_webhook_signature_rejects_mismatch_and_missing():
    body = b"payload"
    assert verify_webhook_signature(body, "deadbeef", "wh-secret") is False
    assert verify_webhook_signature(body, None, "wh-secret") is False
    assert verify_webhook_signature(body, "", "wh-secret") is False
    # No configured secret -> never trust.
    assert verify_webhook_signature(body, "anything", "") is False
