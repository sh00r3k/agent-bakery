"""Dashboard auth — admin-only, shared HS256 (Plan 4 §4).

Two token roles:

1. **Browser session.** A human pastes a host-minted admin token into ``/login``;
   the dashboard verifies it with the shared ``JWT_SECRET`` via
   ``agentkit.auth.verify_token`` and stores the raw JWT in an HttpOnly, Secure,
   SameSite=Strict cookie. Every page depends on ``require_session`` which reads
   the cookie (instead of the ``Authorization`` header) and re-verifies.

2. **Upstream service token.** To call sibling agents' admin endpoints the
   dashboard mints its OWN short-lived token ``{sub: dashboard, tenant: platform,
   role: admin, exp}`` with the same secret, cached and refreshed before expiry.

The dashboard never issues login credentials of its own (consistent with "agents
never run login") — it only verifies a token the host already signed.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import jwt
from agentkit.auth import AuthError, Principal, verify_token
from fastapi import HTTPException, Request, status

from .settings import Settings


def verify_admin_token(token: str, settings: Settings) -> Principal:
    """Verify a host token and require an admin/manager role. Raises 401/403."""
    principal = verify_token(
        token,
        secret=settings.jwt_secret,
        algorithms=settings.jwt_algorithms,
        audience=settings.jwt_audience,
    )
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return principal


def require_session(settings: Settings) -> Callable[[Request], Principal]:
    """Build a FastAPI dependency that authenticates from the session cookie.

    Returns the verified admin ``Principal``. On any failure raises 401 — the
    page layer turns that into a redirect to ``/login``.
    """

    def _dep(request: Request) -> Principal:
        token = request.cookies.get(settings.session_cookie_name)
        if not token:
            raise AuthError("no session")
        return verify_admin_token(token, settings)

    return _dep


class UpstreamToken:
    """Mints + caches the dashboard's own service admin token for upstream calls.

    HS256 over the shared secret, ``role=admin`` so it passes every agent's
    ``require_admin`` / ``/metrics.json`` guard. Refreshed when within 60s of exp.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: str | None = None
        self._exp: float = 0.0

    def get(self) -> str:
        now = time.time()
        if self._token is not None and now < self._exp - 60:
            return self._token
        ttl = self._settings.upstream_token_ttl_s
        exp = int(now) + ttl
        claims = {
            "sub": self._settings.upstream_token_sub,
            "tenant": "platform",
            "role": "admin",
            "exp": exp,
        }
        aud = self._settings.jwt_audience
        if aud:
            claims["aud"] = aud
        self._token = jwt.encode(claims, self._settings.jwt_secret, algorithm="HS256")
        self._exp = exp
        return self._token

    def auth_header(self) -> dict[str, str]:
        if not self._settings.jwt_secret:
            return {}
        return {"Authorization": f"Bearer {self.get()}"}
