"""Personal Access Token routes (Pattern 3 — Personal Access Tokens).

A small ``APIRouter`` imported at the BOTTOM of ``api.py`` (like ``panel_routes``)
because it imports ``page_principal``/``templates``/``_session_dep`` from there.

- ``GET  /partials/tokens``       — the token list (tenant-scoped; never a secret)
- ``POST /actions/token/mint``    — mint a token; one-time secret reveal
- ``POST /actions/token/revoke``  — revoke a token (tenant-scoped)

Every route is admin-gated (``_session_dep`` already requires admin/manager;
a defensive ``is_admin`` check is kept) and degrades to an "unavailable" state
when the DB pool is ``None``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentkit import audit
from agentkit.auth import Principal
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from psycopg import errors as pg_errors

from . import pat, store
from .api import _session_dep, templates

router = APIRouter(tags=["tokens"])

# Role rank for mint clamping: a minter can never issue a token more privileged
# than themselves (BR-002 — no privilege escalation via PAT).
_ROLE_RANK = {"viewer": 0, "manager": 1, "admin": 2}
_DEFAULT_ROLE = "viewer"


def _clamp_role(requested: str, *, minter_role: str) -> str:
    req_rank = _ROLE_RANK.get(requested, 0)
    minter_rank = _ROLE_RANK.get(minter_role, 0)
    role = requested if requested in _ROLE_RANK else _DEFAULT_ROLE
    return role if req_rank <= minter_rank else minter_role


@router.get("/partials/tokens", response_class=HTMLResponse)
async def partial_tokens(request: Request, p: Principal = Depends(_session_dep)) -> HTMLResponse:
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="admin required")
    pool = request.app.state.pool
    if pool is None:
        return templates.TemplateResponse(
            request,
            "partials/tokens.html",
            {"unavailable": "database is unavailable", "tokens": []},
        )
    tokens = await store.list_pats(pool, tenant_id=p.tenant)
    return templates.TemplateResponse(
        request, "partials/tokens.html", {"unavailable": None, "tokens": tokens}
    )


@router.post("/actions/token/mint", response_class=HTMLResponse)
async def mint_token(
    request: Request,
    name: str = Form(...),
    scope: str = Form("read:agents"),
    role: str = Form(_DEFAULT_ROLE),
    expires_days: int = Form(30),
    p: Principal = Depends(_session_dep),
) -> HTMLResponse:
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="admin required")
    pool = request.app.state.pool
    if pool is None:
        raise HTTPException(status_code=503, detail="token store is unavailable")
    label = (name or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="name is required")
    days = max(1, min(int(expires_days or 30), 365))
    expires_at = datetime.now(UTC) + timedelta(days=days)
    effective_role = _clamp_role(role, minter_role=p.role)
    scope_label = (scope or "read:agents").strip()

    # Retry on the (astronomically unlikely) prefix UNIQUE collision rather than
    # letting a raw psycopg UniqueViolation escape as a 500.
    secret = ""
    for _attempt in range(5):
        secret, prefix, token_hash = pat.mint_pat()
        try:
            await store.create_pat(
                pool,
                tenant_id=p.tenant,
                prefix=prefix,
                token_hash=token_hash,
                name=label,
                scope=scope_label,
                role=effective_role,
                created_by=p.sub,
                expires_at=expires_at,
            )
            break
        except pg_errors.UniqueViolation:
            continue
    else:
        raise HTTPException(status_code=500, detail="could not allocate a unique token prefix")

    await audit.append(
        pool,
        tenant_id=p.tenant,
        actor=p.sub,
        action="token-mint",
        resource=label,
        metadata={"scope": scope_label, "role": effective_role},
    )
    # The plaintext secret is rendered exactly once, here, and never stored.
    return templates.TemplateResponse(
        request,
        "partials/token_reveal.html",
        {"secret": secret, "name": label, "role": effective_role, "expires_at": expires_at},
    )


@router.post("/actions/token/revoke", response_class=HTMLResponse)
async def revoke_token(
    request: Request, id: int = Form(...), p: Principal = Depends(_session_dep)
) -> HTMLResponse:
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="admin required")
    pool = request.app.state.pool
    if pool is None:
        raise HTTPException(status_code=503, detail="token store is unavailable")
    revoked = await store.revoke_pat(pool, tenant_id=p.tenant, token_id=id)
    if revoked:
        await audit.append(
            pool, tenant_id=p.tenant, actor=p.sub, action="token-revoke", resource=str(id)
        )
    tokens = await store.list_pats(pool, tenant_id=p.tenant)
    return templates.TemplateResponse(
        request, "partials/tokens.html", {"unavailable": None, "tokens": tokens}
    )
