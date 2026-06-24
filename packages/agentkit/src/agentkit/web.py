"""FastAPI auth adapters — the framework-bound half of the auth contract.

The pure contract (``Principal``, ``verify_token``, ``verify_webhook_signature``)
lives in :mod:`agentkit.auth` with no web-framework import. This module holds the
adapters that *do* need FastAPI: ``make_principal_dependency`` builds a
``Depends``-able header authenticator bound to an agent's settings, and
``require_admin`` enforces the admin role. Splitting them out keeps the domain
contract importable without pulling FastAPI in (ARCH-002).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import Header, HTTPException, status

from agentkit.auth import AuthError, Principal, verify_token

if TYPE_CHECKING:
    from agentkit.config import BaseAgentSettings


def make_principal_dependency(
    settings: BaseAgentSettings,
) -> Callable[[str], Principal]:
    """Build a FastAPI dependency bound to this agent's settings.

    Usage in an agent::

        from fastapi import Depends

        current_principal = make_principal_dependency(settings)

        @app.get("/whoami")
        def whoami(p: Principal = Depends(current_principal)):
            return {"sub": p.sub, "tenant": p.tenant}
    """

    def _dep(authorization: str = Header(default="")) -> Principal:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError("missing bearer token")
        return verify_token(
            token,
            secret=settings.jwt_secret,
            algorithms=settings.jwt_algorithms,
            audience=settings.jwt_audience,
        )

    return _dep


def require_admin(principal: Principal) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return principal


__all__ = ["make_principal_dependency", "require_admin"]
