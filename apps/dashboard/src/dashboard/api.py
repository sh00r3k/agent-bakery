"""HTTP surface for the unified agents dashboard (ADR-0002).

Server-rendered (FastAPI + Jinja2 + HTMX). The dashboard is a *peer agent* built
on agentkit — it inherits ``/healthz``, ``/readyz`` and ``/metrics.json`` from
``agentkit.server.create_app`` (so meta-monitoring can watch the dashboard too,
Plan 4 §7) — but its "graph" is HTTP fan-out across the agents, not an LLM graph.

Routes:
- ``GET  /login`` / ``POST /login`` / ``POST /logout`` — cookie session over the
  shared HS256 admin token (Plan 4 §4). No login DB; verifies a host-minted JWT.
- ``GET  /``, ``/incidents``, ``/findings``, ``/pipeline``, ``/pm``,
  ``/cost`` — the screens; each renders a shell that HTMX-polls a partial.
- ``GET  /partials/*`` — the polled HTMX fragments (Plan 4 §5 polling).
- ``POST /actions/sweep`` / ``/actions/scan`` — proxy admin actions to monitoring
  / security with the dashboard's own upstream token.

Every page/partial/action depends on the session cookie; unauthenticated page
loads redirect to ``/login``, unauthenticated fragment/action calls 401.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import hmac
import io
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from agentkit import audit, create_app, get_logger
from agentkit import db as agentdb
from agentkit.auth import AuthError, Principal
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg_pool import AsyncConnectionPool

from . import store, views
from .aggregator import Aggregator, AlreadyRunning, Unavailable
from .auth import UpstreamToken, require_session, verify_admin_token
from .registry import build_registry, with_feature
from .settings import get_settings

log = get_logger("dashboard.api")
settings = get_settings()

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _asset_version() -> str:
    """A cache-busting token = newest mtime of the CSS assets, so a redeploy
    forces browsers to refetch dash.css/tokens.css instead of serving a stale
    cached copy. Computed once at import (the files don't change at runtime)."""
    static = _HERE / "static"
    try:
        mtimes = [(static / f).stat().st_mtime for f in ("dash.css", "tokens.css")]
        return str(int(max(mtimes)))
    except OSError:
        return "0"


# Exposed to every template as {{ asset_v }} for versioned <link> hrefs.
templates.env.globals["asset_v"] = _asset_version()
# Brand shown in the masthead / login / <title> (config/env-driven, generic default).
templates.env.globals["brand"] = get_settings().brand
registry = build_registry(settings)
# Explicitly annotated so the bottom-imported sub-routers (panel_routes,
# pat_routes) can resolve its type across the import cycle (mypy has-type).
_session_dep: Callable[[Request], Principal] = require_session(settings)


# --- CSRF: signed double-submit token (Plan 4 §4 hardening) -----------------
# SameSite=Strict already blocks the simplest cross-site POSTs, but is not a
# complete defense (older browsers, same-site subdomains). We layer a signed
# double-submit token: an HMAC-signed random token lives in a readable cookie
# AND must be echoed back on every state-changing request (hidden form field for
# plain forms, ``X-CSRF-Token`` header for HTMX). The handler validates the
# signature, constant-time-compares the echoed value to the cookie, and verifies
# Origin/Referer when present. Mismatch => 403.
CSRF_FORM_FIELD = "csrf_token"  # hidden form field name (not a secret)
CSRF_HEADER = "X-CSRF-Token"  # HTMX request header name (not a secret)
# Methods that mutate state and therefore require a valid CSRF token.
_CSRF_PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Ops endpoints inherited from agentkit (health/metrics) are not browser-driven
# and are exempt from CSRF.
_CSRF_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics.json"})


def _csrf_exempt(path: str) -> bool:
    """True for paths excluded from CSRF: the exact ops endpoints. Login and the
    dashboard ``/actions/*`` paths are NOT exempt — they stay CSRF-protected."""
    return path in _CSRF_EXEMPT_PATHS


# Signing key for the CSRF token, derived from the shared JWT secret (always set
# in any real deployment). Stdlib HMAC-SHA256 keeps this dependency-free; the
# token is ``<nonce>.<hmac-hex>`` — the nonce is opaque random bytes and the
# signature is what makes the readable cookie unforgeable by a cross-origin
# attacker who can submit but cannot read it.
_CSRF_KEY = hashlib.sha256(
    b"dashboard-csrf-v1:" + (settings.jwt_secret or "dashboard-csrf-unconfigured").encode()
).digest()


def _csrf_sign(nonce: str) -> str:
    return hmac.new(_CSRF_KEY, nonce.encode(), hashlib.sha256).hexdigest()


def issue_csrf_token() -> str:
    """Mint a fresh signed CSRF token: ``<random nonce>.<HMAC-SHA256 hex>``."""
    nonce = secrets.token_urlsafe(32)
    return f"{nonce}.{_csrf_sign(nonce)}"


def _csrf_valid(token: str | None) -> bool:
    """True iff ``token`` is well-formed and its signature verifies (const-time)."""
    if not token or "." not in token:
        return False
    nonce, _, sig = token.rpartition(".")
    if not nonce or not sig:
        return False
    return hmac.compare_digest(sig, _csrf_sign(nonce))


def _origin_ok(request: Request) -> bool:
    """Verify Origin/Referer host matches the request host when present.

    A missing Origin AND Referer is tolerated (some legitimate same-origin
    clients omit both); the signed double-submit token is the primary guard.
    When either is present it MUST match the request's host:port.
    """
    host = request.headers.get("host")
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.netloc and host and parsed.netloc != host:
            return False
    return True


async def _buffered_body(request: Request) -> bytes:
    """Read the request body once and re-arm ``request._receive`` to replay it.

    Needed because reading the body to inspect the CSRF field would otherwise
    drain the ASGI receive channel under ``BaseHTTPMiddleware``, leaving the
    downstream ``Form(...)`` handler with an empty body (a spurious 422). We
    parse the field from these bytes directly (never calling ``request.form()``,
    which would consume the replay) so the single replay is preserved for the
    handler. Idempotent per request.
    """
    body = await request.body()
    sent = False

    async def _replay() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = _replay
    return body


async def _submitted_csrf(request: Request) -> str | None:
    """The token echoed back: ``X-CSRF-Token`` header (HTMX) or the hidden
    ``csrf_token`` field of a urlencoded form. The form bytes are buffered +
    replayed so the downstream handler still parses the body. (The dashboard
    posts only urlencoded forms; multipart isn't used.)"""
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/x-www-form-urlencoded"):
        body = await _buffered_body(request)
        fields = parse_qs(body.decode("utf-8", "replace"))
        values = fields.get(CSRF_FORM_FIELD)
        return values[0] if values else None
    return None


async def csrf_check(request: Request) -> str | None:
    """Validate CSRF for a state-changing request; return the rejection reason
    (a short string) or ``None`` when the request passes. Non-mutating methods
    and exempt paths always pass."""
    if request.method not in _CSRF_PROTECTED_METHODS:
        return None
    if _csrf_exempt(request.url.path):
        return None
    if not _origin_ok(request):
        return "bad origin"
    cookie = request.cookies.get(settings.csrf_cookie_name)
    submitted = await _submitted_csrf(request)
    if not _csrf_valid(cookie) or not _csrf_valid(submitted):
        return "missing CSRF token"
    if not hmac.compare_digest(cookie or "", submitted or ""):
        return "CSRF token mismatch"
    return None


def _set_csrf_cookie(response: Response, token: str) -> None:
    """Attach the readable (non-HttpOnly) CSRF cookie carrying ``token``."""
    response.set_cookie(
        settings.csrf_cookie_name,
        token,
        httponly=False,  # JS/HTMX must read it for the double-submit echo
        secure=settings.effective_session_cookie_secure,
        samesite="strict",
    )


async def _open_pool(stack: contextlib.AsyncExitStack) -> AsyncConnectionPool | None:
    """Open the dashboard's own Postgres pool with a bounded timeout.

    Returns the pool, or ``None`` if the DB is unreachable at boot — in which
    case the dashboard still serves (it reads agents over HTTP); only its own
    heartbeat/cost time-series is skipped until the DB returns. Bounded because
    the pool's ``open(wait=True)`` otherwise retries indefinitely and would wedge
    startup, leaving nothing listening on the port (and failing the healthcheck).
    """
    try:
        return await asyncio.wait_for(
            stack.enter_async_context(agentdb.pg_pool(settings)), timeout=10.0
        )
    except (TimeoutError, Exception) as exc:
        log.warning("lifespan.db_unavailable", error=str(exc))
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    token = UpstreamToken(settings)
    redis = agentdb.redis_client(settings)
    app.state.token = token
    app.state.redis = redis
    app.state.aggregator = Aggregator(settings, token, redis=redis)
    app.state.registry = registry
    bg_tasks: list[asyncio.Task[None]] = []
    async with contextlib.AsyncExitStack() as stack:
        pool = await _open_pool(stack)
        app.state.pool = pool
        if pool is not None:
            with contextlib.suppress(Exception):
                await store.create_schema(pool)
                await audit.create_audit_schema(pool)
                bg_tasks.append(asyncio.create_task(_snapshot_loop(app)))
                bg_tasks.append(asyncio.create_task(_prune_loop(app)))
        try:
            yield
        finally:
            for task in bg_tasks:
                task.cancel()
            for task in bg_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await app.state.aggregator.aclose()
            with contextlib.suppress(Exception):
                await redis.aclose()


async def _snapshot_loop(app: FastAPI, *, interval_s: int = 60) -> None:
    """Persist a health + cost snapshot per agent into the dashboard's own DB
    (Plan 4 §7.2 heartbeats / §3.4 cost). Cancelled on shutdown."""
    agg: Aggregator = app.state.aggregator
    pool = app.state.pool
    while True:
        try:
            # refresh=True so the snapshot records the *current* health (not a
            # possibly-stale cached tile) while coherently re-warming the shared
            # cache that the UI reads — instead of racing a separate cold fan-out.
            agents = await agg.agents_health(registry, refresh=True)
            for h in agents:
                if h.kind == "self":
                    continue
                await store.record_heartbeat(
                    pool,
                    agent=h.slug,
                    up=h.live,
                    ready=h.ready,
                    uptime_s=h.uptime_s,
                    error_rate_5m=h.error_rate_5m,
                    requests_5m=h.requests_5m,
                )
                if h.llm_cost_usd_today is not None:
                    await store.record_cost(pool, agent=h.slug, usd_today=h.llm_cost_usd_today)
                if h.cost_by_model_today:
                    await store.record_cost_by_model(
                        pool, agent=h.slug, by_model=h.cost_by_model_today
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("snapshot.failed", error=str(exc))
        await asyncio.sleep(interval_s)


async def _prune_loop(
    app: FastAPI,
    *,
    interval_s: int = 6 * 60 * 60,
    retention_days: int = store.DEFAULT_RETENTION_DAYS,
) -> None:
    """Periodically prune the unbounded raw snapshot logs (heartbeats / cost
    events) past the retention horizon. The compact ``cost_daily`` rollup is kept
    indefinitely, so the cost charts stay intact while the raw tables don't grow
    without bound. Runs every ``interval_s`` (default 6h); cancelled on shutdown."""
    pool = app.state.pool
    while True:
        try:
            await store.rollup_cost_daily(pool)
            await store.prune_old_data(pool, days=retention_days)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("prune.failed", error=str(exc))
        await asyncio.sleep(interval_s)


# create_app is an untyped agentkit factory (returns Any); pin the result to
# FastAPI so the @app route decorators below stay typed under strict mypy.
app: FastAPI = create_app(settings, title="dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


@app.middleware("http")
async def _csrf_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Enforce CSRF on state-changing requests, and ensure every response carries
    a readable CSRF cookie so the next form/HTMX POST can echo it back.

    Runs ahead of the route handlers; a rejection short-circuits with 403 before
    any side effect. On the way out it (re)issues the cookie when absent so a
    freshly arrived browser (e.g. the GET /login page) gets a usable token."""
    reason = await csrf_check(request)
    if reason is not None:
        log.warning("csrf.rejected", path=request.url.path, reason=reason)
        return Response(content=reason, status_code=status.HTTP_403_FORBIDDEN)
    response = await call_next(request)
    # (Re)issue the cookie when the browser presents NONE — or one that no longer
    # VALIDATES (a foreign/corrupted/truncated cookie, or one signed under a since-
    # rotated jwt_secret). A presence-only check would keep refusing to overwrite a
    # now-invalid cookie: the form would carry a fresh valid token while the stale
    # cookie never matches, so every POST (including POST /login) 403s forever until
    # the user manually clears cookies. Re-issuing on invalidity breaks that lockout.
    # Prefer the token a handler already minted into request.state.csrf_issued so the
    # cookie and the embedded form field carry the SAME token (double-submit match).
    presented = request.cookies.get(settings.csrf_cookie_name)
    if not _csrf_valid(presented):
        issued = getattr(request.state, "csrf_issued", None)
        _set_csrf_cookie(response, issued if isinstance(issued, str) else issue_csrf_token())
    return response


def _csrf_token(request: Request) -> str:
    """The CSRF token to embed in a rendered form/page: the one the browser
    already holds (so the cookie and the field match), or a fresh one when the
    browser has none yet (cached on request.state so the middleware sets the
    SAME value in the cookie on the way out)."""
    existing = request.cookies.get(settings.csrf_cookie_name)
    if _csrf_valid(existing):
        return existing or ""
    issued = issue_csrf_token()
    request.state.csrf_issued = issued
    return issued


def _agg(request: Request) -> Aggregator:
    return cast("Aggregator", request.app.state.aggregator)


async def _audit(
    request: Request,
    principal: Principal,
    action: str,
    resource: str | None = None,
    **meta: Any,
) -> None:
    """Append a best-effort audit row for a state-changing action (Pattern 4).
    No-op when the DB pool is None; never raises into the request path."""
    await audit.append(
        request.app.state.pool,
        tenant_id=principal.tenant,
        actor=principal.sub,
        action=action,
        resource=resource,
        metadata=meta or None,
    )


# --- auth: page dependency that redirects, fragment dependency that 401s ----
def page_principal(request: Request) -> Principal:
    try:
        return _session_dep(request)
    except AuthError as exc:
        raise _Redirect("/login") from exc
    except HTTPException as exc:
        # A valid-but-non-admin token raises a plain 403 (not an AuthError);
        # bounce the page layer to /login rather than serving a bare 403.
        if exc.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
            raise _Redirect("/login") from exc
        raise


class _Redirect(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


@app.exception_handler(_Redirect)
async def _on_redirect(request: Request, exc: _Redirect) -> RedirectResponse:
    return RedirectResponse(exc.location, status_code=status.HTTP_303_SEE_OTHER)


# --- login / logout ---------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "csrf_token": _csrf_token(request)}
    )


@app.post("/login")
async def login_submit(request: Request, token: str = Form(...)) -> Response:
    try:
        principal = verify_admin_token(token, settings)
    except Exception:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "invalid or non-admin token", "csrf_token": _csrf_token(request)},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    await _audit(request, principal, "login", principal.sub)
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        settings.session_cookie_name,
        token,
        httponly=True,
        secure=settings.effective_session_cookie_secure,
        samesite="strict",
    )
    # Rotate the CSRF token on the new session (defense against session fixation
    # of the CSRF nonce); the readable cookie + same value feed the next POST.
    _set_csrf_cookie(resp, issue_csrf_token())
    return resp


@app.post("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(settings.session_cookie_name)
    resp.delete_cookie(settings.csrf_cookie_name)
    return resp


# --- pages (shells that HTMX-poll a partial) --------------------------------
def _page_ctx(request: Request, p: Principal) -> dict[str, Any]:
    """Base template context shared by every page shell.

    Carries the principal + the workspace-switcher data (Pattern 1) so the
    top-bar switcher renders on every page from one place, plus the per-session
    ``csrf_token`` that base.html emits as the global HTMX ``hx-headers`` (so
    every HTMX POST on the page — and its partials — echoes it) and the logout
    form's hidden field. v0.2 exposes a single workspace (the principal's
    tenant); cross-tenant switching is v0.3.
    """
    return {
        "principal": p,
        "env": settings.env,
        "csrf_token": _csrf_token(request),
        "workspaces": [{"tenant_id": p.tenant, "display_name": p.tenant, "is_current": True}],
        "current_tenant_id": p.tenant,
    }


def _page(name: str, active: str) -> Callable[..., Awaitable[Response]]:
    async def _handler(request: Request, p: Principal = Depends(page_principal)) -> Response:
        ctx = _page_ctx(request, p)
        ctx["active"] = active
        return templates.TemplateResponse(request, name, ctx)

    return _handler


app.add_api_route(
    "/", _page("overview.html", "overview"), methods=["GET"], response_class=HTMLResponse
)
app.add_api_route(
    "/incidents", _page("incidents.html", "incidents"), methods=["GET"], response_class=HTMLResponse
)
app.add_api_route(
    "/findings", _page("findings.html", "findings"), methods=["GET"], response_class=HTMLResponse
)


app.add_api_route(
    "/pipeline", _page("pipeline.html", "pipeline"), methods=["GET"], response_class=HTMLResponse
)
app.add_api_route("/pm", _page("pm.html", "pm"), methods=["GET"], response_class=HTMLResponse)
app.add_api_route("/cost", _page("cost.html", "cost"), methods=["GET"], response_class=HTMLResponse)
app.add_api_route("/qa", _page("qa.html", "qa"), methods=["GET"], response_class=HTMLResponse)
# v0.2 UI pages (node health, workspace members, activity, tokens, privacy).
app.add_api_route(
    "/agents", _page("agents.html", "agents"), methods=["GET"], response_class=HTMLResponse
)
app.add_api_route(
    "/workspace", _page("workspace.html", "workspace"), methods=["GET"], response_class=HTMLResponse
)
app.add_api_route(
    "/activity", _page("activity.html", "activity"), methods=["GET"], response_class=HTMLResponse
)
app.add_api_route(
    "/tokens", _page("tokens.html", "tokens"), methods=["GET"], response_class=HTMLResponse
)
app.add_api_route(
    "/privacy", _page("privacy.html", "privacy"), methods=["GET"], response_class=HTMLResponse
)


# --- partials (HTMX fragments) ----------------------------------------------
@app.get("/partials/overview", response_class=HTMLResponse)
async def partial_overview(request: Request, _: Principal = Depends(_session_dep)) -> Response:
    ctx = await views.overview_context(_agg(request), registry, pool=request.app.state.pool)
    ctx["env"] = settings.env
    return templates.TemplateResponse(request, "partials/overview.html", ctx)


@app.get("/partials/incidents", response_class=HTMLResponse)
async def partial_incidents(request: Request, p: Principal = Depends(_session_dep)) -> Response:
    ctx = await views.incidents_context(
        _agg(request), registry, pool=request.app.state.pool, tenant=p.tenant
    )
    return templates.TemplateResponse(request, "partials/incidents.html", ctx)


@app.get("/partials/findings", response_class=HTMLResponse)
async def partial_findings(
    request: Request, severity: str | None = None, _: Principal = Depends(_session_dep)
) -> Response:
    ctx = await views.findings_context(_agg(request), registry, severity=severity)
    return templates.TemplateResponse(request, "partials/findings.html", ctx)


@app.get("/partials/qa", response_class=HTMLResponse)
async def partial_qa(
    request: Request, severity: str | None = None, _: Principal = Depends(_session_dep)
) -> Response:
    ctx = await views.qa_context(_agg(request), registry, severity=severity)
    return templates.TemplateResponse(request, "partials/qa.html", ctx)


@app.get("/partials/pipeline", response_class=HTMLResponse)
async def partial_pipeline(request: Request, _: Principal = Depends(_session_dep)) -> Response:
    ctx = await views.pipeline_context(_agg(request), registry)
    return templates.TemplateResponse(request, "partials/pipeline.html", ctx)


@app.get("/partials/pm", response_class=HTMLResponse)
async def partial_pm(request: Request, _: Principal = Depends(_session_dep)) -> Response:
    ctx = await views.pm_context(_agg(request), registry)
    return templates.TemplateResponse(request, "partials/pm.html", ctx)


@app.get("/partials/cost", response_class=HTMLResponse)
async def partial_cost(request: Request, _: Principal = Depends(_session_dep)) -> Response:
    ctx = await views.cost_context(_agg(request), registry, pool=request.app.state.pool)
    return templates.TemplateResponse(request, "partials/cost.html", ctx)


@app.get("/partials/activity", response_class=HTMLResponse)
async def partial_activity(
    request: Request,
    action: str | None = None,
    actor: str | None = None,
    q: str | None = None,
    before: str | None = None,
    p: Principal = Depends(_session_dep),
) -> Response:
    """Tenant-scoped activity feed (Pattern 4) with filters (AU-1). The tenant is
    the principal's — NEVER a query param (BR-002). ``action``/``actor``/``q``
    filter; ``before`` (ISO ts) is the load-older cursor."""
    before_dt: datetime | None = None
    if before:
        with contextlib.suppress(ValueError):
            before_dt = datetime.fromisoformat(before)
    ctx = await views.activity_context(
        request.app.state.pool, tenant=p.tenant, action=action, actor=actor, q=q, before=before_dt
    )
    return templates.TemplateResponse(request, "partials/activity.html", ctx)


@app.get("/partials/workspace", response_class=HTMLResponse)
async def partial_workspace(request: Request, p: Principal = Depends(_session_dep)) -> Response:
    """Workspace members + capabilities (Pattern 2), scoped to the principal."""
    ctx = views.workspace_context(registry, principal=p)
    return templates.TemplateResponse(request, "partials/workspace.html", ctx)


@app.get("/partials/privacy", response_class=HTMLResponse)
async def partial_privacy(request: Request, _: Principal = Depends(_session_dep)) -> Response:
    """Local-first trust panel — Private Mode state + egress allow-list."""
    ctx = views.privacy_context(settings)
    return templates.TemplateResponse(request, "partials/privacy.html", ctx)


# --- agent detail (AG-1): per-agent drilldown off the overview/agents tiles ----
@app.get("/agents/{slug}", response_class=HTMLResponse)
async def agent_detail_page(
    request: Request, slug: str, p: Principal = Depends(page_principal)
) -> Response:
    ctx = _page_ctx(request, p)
    ctx.update(active="agents", slug=slug)
    return templates.TemplateResponse(request, "agent_detail.html", ctx)


@app.get("/partials/agent/{slug}", response_class=HTMLResponse)
async def partial_agent(
    request: Request, slug: str, _: Principal = Depends(_session_dep)
) -> Response:
    ctx = await views.agent_detail_context(
        _agg(request), registry, slug=slug, pool=request.app.state.pool
    )
    return templates.TemplateResponse(request, "partials/agent_detail.html", ctx)


_AUDIT_COLUMNS = ("id", "ts", "actor", "action", "resource", "metadata")


@app.get("/audit/export")
async def audit_export(
    request: Request, format: str = "json", p: Principal = Depends(_session_dep)
) -> StreamingResponse:
    """Export the tenant's audit log as JSON or CSV (Pattern 5).

    Tenant-scoped to the principal (never a query param, BR-002). 400 on a bad
    format, 503 when the DB pool is unavailable. Streams row-by-row.
    """
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be json or csv")
    pool = request.app.state.pool
    if pool is None:
        raise HTTPException(status_code=503, detail="audit log database is unavailable")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"audit-{p.tenant}-{stamp}.{format}"

    async def _json_rows() -> AsyncIterator[str]:
        yield "["
        first = True
        async for row in store.iter_audit_log(pool, tenant_id=p.tenant):
            row["ts"] = row["ts"].isoformat() if row["ts"] is not None else None
            yield ("" if first else ",") + json.dumps(row, default=str)
            first = False
        yield "]"

    async def _csv_rows() -> AsyncIterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_AUDIT_COLUMNS)
        async for row in store.iter_audit_log(pool, tenant_id=p.tenant):
            writer.writerow(
                [
                    row["id"],
                    row["ts"].isoformat() if row["ts"] is not None else "",
                    row["actor"],
                    row["action"],
                    row["resource"] or "",
                    json.dumps(row["metadata"], default=str),
                ]
            )
        buf.seek(0)
        yield buf.getvalue()

    media = "application/json" if format == "json" else "text/csv"
    rows = _json_rows() if format == "json" else _csv_rows()
    return StreamingResponse(
        rows,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- actions (proxy admin POSTs to siblings) --------------------------------
@app.post("/actions/sweep", response_class=HTMLResponse)
async def action_sweep(request: Request, p: Principal = Depends(_session_dep)) -> Response:
    spec = with_feature(registry, "incidents")
    res = await _agg(request).run_sweep(spec) if spec else Unavailable("incidents", "no agent")
    await _audit(request, p, "sweep", "incidents", ok=not isinstance(res, Unavailable))
    return _action_result(request, "sweep", res)


@app.post("/actions/scan", response_class=HTMLResponse)
async def action_scan(request: Request, p: Principal = Depends(_session_dep)) -> Response:
    spec = with_feature(registry, "findings")
    res = await _agg(request).run_scan(spec) if spec else Unavailable("findings", "no agent")
    await _audit(request, p, "scan", "findings", ok=not isinstance(res, Unavailable))
    return _action_result(request, "scan", res)


# Mirror of the ultraQA agent's FEEDBACK_STATUSES (its /findings/resolve gate).
# Kept local so the dashboard doesn't import the agent package; the agent
# re-validates server-side (a stale value here just yields a benign upstream 400).
_FINDING_RESOLVE_STATUSES = frozenset({"fixed", "dismissed", "wontfix", "confirmed", "needs_human"})


@app.post("/actions/finding/resolve", response_class=HTMLResponse)
async def action_finding_resolve(
    request: Request,
    dedup_key: str = Form(...),
    status: str = Form(...),
    source: str = Form("findings"),
    note: str | None = Form(None),
    p: Principal = Depends(_session_dep),
) -> Response:
    """Triage one finding (F-1/F-2): proxy the operator's decision to the owning
    agent's /findings/resolve, then re-render the list (the resolved row drops
    out of the signal view). ``source`` picks the agent — the security-findings
    feed or the ultraQA tester."""
    if status not in _FINDING_RESOLVE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid finding status")
    dedup_key = dedup_key.strip()
    if not dedup_key:
        raise HTTPException(status_code=400, detail="dedup_key required")
    note = (note or "").strip()[:2000] or None
    # ``source=="qa"`` targets the coverage-capable QA tester; otherwise the
    # security findings agent. Both resolved by capability flag, not a slug.
    spec = with_feature(registry, "coverage" if source == "qa" else "findings")
    res: dict[str, Any] | Unavailable
    if spec is None:
        res = Unavailable(source, "no agent")
    else:
        res = await _agg(request).resolve_finding(
            spec, dedup_key=dedup_key, status=status, by=p.sub, note=note
        )
    await _audit(
        request,
        p,
        "finding-resolve",
        dedup_key,
        status=status,
        source=source,
        ok=not isinstance(res, Unavailable),
    )
    if source == "qa":
        ctx = await views.qa_context(_agg(request), registry)
        return templates.TemplateResponse(request, "partials/qa.html", ctx)
    ctx = await views.findings_context(_agg(request), registry)
    return templates.TemplateResponse(request, "partials/findings.html", ctx)


@app.post("/actions/incident/triage", response_class=HTMLResponse)
async def action_incident_triage(
    request: Request,
    dedup_key: str = Form(...),
    status: str = Form(...),
    note: str | None = Form(None),
    p: Principal = Depends(_session_dep),
) -> Response:
    """Triage one incident (I-1): ack/resolve/snooze/reopen. The monitoring agent
    exposes no such endpoint, so the decision persists in the dashboard's own
    overlay (tenant-scoped), audited, then the list re-renders with the badge."""
    if status not in store.INCIDENT_TRIAGE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid incident status")
    dedup_key = dedup_key.strip()
    if not dedup_key:
        raise HTTPException(status_code=400, detail="dedup_key required")
    note = (note or "").strip()[:2000] or None
    await store.set_incident_triage(
        request.app.state.pool,
        tenant_id=p.tenant,
        dedup_key=dedup_key,
        status=status,
        actor=p.sub,
        note=note,
    )
    await _audit(request, p, "incident-triage", dedup_key, status=status)
    ctx = await views.incidents_context(
        _agg(request), registry, pool=request.app.state.pool, tenant=p.tenant
    )
    return templates.TemplateResponse(request, "partials/incidents.html", ctx)


@app.post("/actions/qa/scan", response_class=HTMLResponse)
async def action_qa_scan(request: Request, p: Principal = Depends(_session_dep)) -> Response:
    """Trigger an on-demand QA sweep (F-2). Distinct from /actions/scan (the
    security-findings agent) — this targets the coverage-capable QA tester,
    resolved by capability flag rather than a literal slug."""
    spec = with_feature(registry, "coverage")
    res = await _agg(request).run_scan(spec) if spec else Unavailable("qa", "no agent")
    await _audit(request, p, "qa-scan", "qa", ok=not isinstance(res, Unavailable))
    return _action_result(request, "QA scan", res)


@app.post("/actions/webext/run", response_class=HTMLResponse)
async def action_webext_run(request: Request, p: Principal = Depends(_session_dep)) -> Response:
    """Trigger a small bounded web-ext pipeline run via the control server.

    Forwards (with the dashboard's minted admin Bearer) to the control server's
    POST /run with conservative defaults (limit=1). Renders a 'started' toast
    with the run_id, a benign 'already running' notice on 409, or an offline
    notice if the control server is unreachable.
    """
    spec = with_feature(registry, "runs")
    res: dict[str, Any] | AlreadyRunning | Unavailable
    if spec is None:
        res = Unavailable("runs", "no agent")
    else:
        res = await _agg(request).webext_run(spec, limit=1)
    await _audit(request, p, "webext-run", "runs")
    if isinstance(res, AlreadyRunning):
        return templates.TemplateResponse(
            request,
            "partials/webext_run_result.html",
            {"state": "running", "run_id": None, "reason": None},
        )
    if isinstance(res, Unavailable):
        return templates.TemplateResponse(
            request,
            "partials/webext_run_result.html",
            {"state": "offline", "run_id": None, "reason": res.reason},
        )
    return templates.TemplateResponse(
        request,
        "partials/webext_run_result.html",
        {"state": "started", "run_id": res.get("run_id"), "reason": None},
    )


def _action_result(request: Request, action: str, res: object) -> Response:
    if isinstance(res, Unavailable):
        return templates.TemplateResponse(
            request,
            "partials/action_result.html",
            {"ok": False, "action": action, "reason": res.reason},
        )
    return templates.TemplateResponse(
        request, "partials/action_result.html", {"ok": True, "action": action, "reason": None}
    )


# v0.2 panel routes (workspace switcher + node-health partial) — imported at the
# BOTTOM so panel_routes' `from .api import page_principal, templates` resolves
# against the fully-initialized module (avoids a circular-import failure).
from .panel_routes import router as panel_router  # noqa: E402

app.include_router(panel_router)

# PAT routes (Pattern 3) — bottom-imported for the same reason: pat_routes does
# `from .api import _session_dep, templates`.
from .pat_routes import router as pat_router  # noqa: E402

app.include_router(pat_router)
