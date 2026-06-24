"""HS256 JWT verification — agents never run login, the host mints tokens.

One shared Principal contract across every agent in the platform.
The host (your backend / partner panel / internal tooling) signs a short-lived
token asserting {tenant, sub, role}; the agent verifies the signature with the
shared secret and derives a Principal. No DB lookup, no session.

This module also provides :func:`mint_token` for services that need to issue
tokens (e.g. the dashboard minting ops tokens), and :func:`mint_refresh_token`
/:func:`rotate_tokens` for a short-lived access + long-lived refresh pattern
that reduces the JWT replay window.

This module is the **framework-free** auth contract: ``Principal``,
``verify_token`` and ``verify_webhook_signature`` are pure (stdlib + PyJWT only)
and carry no FastAPI dependency. The FastAPI adapters
(``make_principal_dependency`` / ``require_admin``) live in :mod:`agentkit.web`;
they are re-exported from here at import time for backward compatibility so
existing ``from agentkit.auth import make_principal_dependency`` callers keep
working.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256

import jwt
from starlette.exceptions import HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED


@dataclass(frozen=True)
class Principal:
    sub: str
    tenant: str
    role: str
    name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "manager")


class AuthError(HTTPException):
    """401 on any auth failure.

    Subclasses Starlette's ``HTTPException`` (the ASGI substrate, not the
    application web framework) so FastAPI's built-in handler still renders a
    clean ``401`` with this detail, while keeping the auth contract free of a
    direct ``fastapi`` import.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=HTTP_401_UNAUTHORIZED, detail=detail)


def mint_token(
    *,
    sub: str,
    tenant: str,
    role: str,
    secret: str,
    algorithm: str = "HS256",
    ttl_s: int = 900,
    audience: str | None = None,
    name: str | None = None,
) -> str:
    """Mint a short-lived access JWT. Default TTL = 15 min (reduced from 1h for
    smaller replay window). The caller is responsible for keeping the secret."""
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": sub,
        "tenant": tenant,
        "role": role,
        "iat": now,
        "exp": now + ttl_s,
    }
    if audience:
        claims["aud"] = audience
    if name:
        claims["name"] = name
    return jwt.encode(claims, secret, algorithm=algorithm)


def mint_refresh_token() -> str:
    """Mint an opaque refresh token (cryptographically random, 256 bits)."""
    return secrets.token_hex(32)


_REFRESH_STORE: dict[str, tuple[str, dict[str, str | None], float]] = {}


def rotate_tokens(
    *,
    sub: str,
    tenant: str,
    role: str,
    secret: str,
    algorithm: str = "HS256",
    access_ttl_s: int = 900,
    refresh_ttl_s: int = 86400 * 7,
    audience: str | None = None,
    name: str | None = None,
) -> tuple[str, str]:
    """Issue an access + refresh token pair.

    The access token is a short-lived JWT (default 15 min). The refresh token
    is an opaque random string bound to the same claims. Call
    :func:`refresh_access` with the refresh token to get a new access token
    without re-authenticating.
    """
    access = mint_token(
        sub=sub,
        tenant=tenant,
        role=role,
        secret=secret,
        algorithm=algorithm,
        ttl_s=access_ttl_s,
        audience=audience,
        name=name,
    )
    refresh = mint_refresh_token()
    _REFRESH_STORE[refresh] = (
        secret,
        {"sub": sub, "tenant": tenant, "role": role, "audience": audience, "name": name},
        time.monotonic() + refresh_ttl_s,
    )
    return access, refresh


def refresh_access(
    refresh_token: str,
    *,
    secret: str,
    algorithms: list[str],
    algorithm: str = "HS256",
    access_ttl_s: int = 900,
) -> tuple[str, str]:
    """Exchange a valid refresh token for a new access + refresh pair.

    Raises :class:`AuthError` if the refresh token is unknown or expired.
    The old refresh token is invalidated (one-time use).
    """
    entry = _REFRESH_STORE.pop(refresh_token, None)
    if entry is None:
        raise AuthError("invalid refresh token")
    stored_secret, claims, expires_at = entry
    if secret != stored_secret:
        raise AuthError("invalid refresh token")
    if time.monotonic() > expires_at:
        raise AuthError("refresh token expired")
    sub = claims["sub"]
    tenant = claims["tenant"]
    role = claims["role"]
    if not sub or not tenant or not role:
        raise AuthError("invalid refresh token claims")
    return rotate_tokens(
        sub=sub,
        tenant=tenant,
        role=role,
        secret=secret,
        algorithm=algorithm,
        access_ttl_s=access_ttl_s,
        audience=claims.get("audience"),
        name=claims.get("name"),
    )


def verify_token(
    token: str, *, secret: str, algorithms: list[str], audience: str | None
) -> Principal:
    """Decode + validate a host token. Raises AuthError (401) on any problem."""
    if not secret:
        raise AuthError("auth not configured")
    # When an audience is configured, REQUIRE the `aud` claim and verify it: a
    # token minted for a different service (or with no audience at all) must be
    # rejected. When unset, stay backward-compatible with the shared-secret dev
    # setup (PyJWT skips aud verification entirely when audience is None).
    require = ["exp", "sub"]
    if audience is not None:
        require.append("aud")
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=algorithms,
            audience=audience,
            options={"require": require},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("invalid token") from exc

    # Require an explicit `tenant` claim — do NOT fall back to `iss` (AF-06):
    # an `iss` is an issuer identity, not a tenant authorization scope, and
    # silently treating it as the tenant is an implicit trust expansion once
    # any agent begins enforcing tenant scoping.
    tenant = claims.get("tenant")
    role = claims.get("role")
    if not tenant or not role:
        raise AuthError("token missing tenant/role")
    return Principal(
        sub=str(claims["sub"]), tenant=str(tenant), role=str(role), name=claims.get("name")
    )


def verify_webhook_signature(raw: bytes, header: str | None, secret: str) -> bool:
    """Constant-time HMAC-SHA256 check for inbound webhook bodies (pure, stdlib).

    Computes ``HMAC-SHA256(secret, raw)`` and compares it against the hex digest
    carried in ``header`` using :func:`hmac.compare_digest`, so all agents share
    one signing scheme. An optional ``sha256=`` prefix (GitHub / Sentry style) is
    tolerated. Returns ``False`` — never raises — when the secret is unset, the
    header is missing/blank, or the digests differ; the route layer turns that
    into a 401.
    """
    if not secret or not header:
        return False
    candidate = header.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[len("sha256=") :]
    expected = hmac.new(secret.encode(), raw, sha256).hexdigest()
    return hmac.compare_digest(expected, candidate)


# Backward-compatible re-exports of the FastAPI adapters. These now live in
# `agentkit.web` (which imports the pure symbols above); importing them here at
# module bottom keeps `from agentkit.auth import make_principal_dependency,
# require_admin` working for existing consumers without dragging fastapi into
# the pure path until the adapters are actually used.
from agentkit.web import make_principal_dependency, require_admin  # noqa: E402

__all__ = [
    "AuthError",
    "Principal",
    "make_principal_dependency",
    "mint_refresh_token",
    "mint_token",
    "refresh_access",
    "require_admin",
    "rotate_tokens",
    "verify_token",
    "verify_webhook_signature",
]
