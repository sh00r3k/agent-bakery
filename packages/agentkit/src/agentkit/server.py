"""FastAPI app factory: consistent /healthz, /readyz, logging, error handling.

Agents call ``create_app(settings, lifespan=...)`` and mount their own routers.
This keeps every agent's operational surface identical so the reverse proxy and
monitoring can treat them uniformly.
"""

from __future__ import annotations

import ipaddress
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from agentkit import db
from agentkit.auth import verify_token
from agentkit.metrics import MetricsRegistry
from agentkit.observability import get_logger, setup_observability

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from agentkit.config import BaseAgentSettings

log = get_logger("agentkit.server")

# Paths excluded from the rolling request/error counters: liveness/readiness and
# the metrics surface itself, so a polling dashboard/monitor doesn't dominate the
# 5m window and so a failing /readyz isn't double-counted as an app error.
_UNCOUNTED_PATHS = frozenset({"/healthz", "/readyz", "/metrics.json"})

# Ops paths the rate limiter must never throttle: health/readiness probes and the
# admin-gated metrics surface (the proxy/orchestrator polls these constantly).
_RATE_LIMIT_EXEMPT_PATHS = _UNCOUNTED_PATHS


def _client_ip(request: Request, *, trust_proxy: bool) -> str:
    """Resolve the client IP for rate-limit keying.

    Default: the socket peer (``request.client.host``) — the only value a client
    cannot forge. When ``trust_proxy`` is set the agent sits behind a *single*
    trusted reverse proxy (our nginx, which APPENDS to ``X-Forwarded-For`` via
    ``$proxy_add_x_forwarded_for``); so the trustworthy hop is the **rightmost**
    XFF entry — the one our proxy itself appended for the immediate peer. The
    leftmost entries are attacker-controlled and an attacker could rotate them to
    evade per-IP limiting, so we never key on them.

    The rightmost value is validated as a real IP; if it does not parse (or the
    header is absent) we fall back to the socket peer. This assumes exactly one
    trusted proxy in front of the app — behind N proxies, key on the Nth-from-right
    entry instead.
    """
    if trust_proxy:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            last = xff.rsplit(",", 1)[-1].strip()
            if last:
                try:
                    ipaddress.ip_address(last)
                except ValueError:
                    log.warning("server.xff_unparseable", value=last)
                else:
                    return last
    return request.client.host if request.client else "unknown"


class _TokenBucketLimiter:
    """Per-IP token-bucket rate limiter (ARCH-009).

    Refills ``rate`` tokens/minute up to a burst of ``rate``. Backed by Redis
    when a client is reachable (shared across replicas); falls back to a process
    -local dict otherwise so a Redis outage degrades to per-instance limiting
    rather than failing open. ``rate <= 0`` disables it (caller skips wiring).
    """

    def __init__(self, settings: BaseAgentSettings) -> None:
        self._rate = max(0, int(settings.rate_limit_per_minute))
        self._settings = settings
        # process-local fallback: ip -> (tokens, last_refill_ts)
        self._local: dict[str, tuple[float, float]] = {}
        self._redis: Redis | None = None
        self._redis_failed = False

    async def _redis_client(self) -> Redis | None:
        if self._redis is not None or self._redis_failed:
            return self._redis
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(self._settings.redis_url, decode_responses=True)
            await client.ping()
            self._redis = client
        except Exception:
            self._redis_failed = True
            log.warning("server.ratelimit_redis_unavailable")
        return self._redis

    async def allow(self, ip: str) -> bool:
        if self._rate <= 0:
            return True
        client = await self._redis_client()
        if client is not None:
            try:
                return await self._allow_redis(client, ip)
            except Exception:
                log.warning("server.ratelimit_redis_error")
        return self._allow_local(ip)

    async def _allow_redis(self, client: Redis, ip: str) -> bool:
        now = time.monotonic()
        key = f"agentkit:rl:{self._settings.agent_name}:{ip}"
        refill_per_s = self._rate / 60.0
        # Lua keeps the read-modify-write atomic across replicas.
        script = """
        local tokens = tonumber(redis.call('hget', KEYS[1], 'tokens'))
        local ts = tonumber(redis.call('hget', KEYS[1], 'ts'))
        local now = tonumber(ARGV[1])
        local rate = tonumber(ARGV[2])
        local refill = tonumber(ARGV[3])
        if tokens == nil then tokens = rate; ts = now end
        tokens = math.min(rate, tokens + (now - ts) * refill)
        local allowed = 0
        if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
        redis.call('hset', KEYS[1], 'tokens', tokens, 'ts', now)
        redis.call('expire', KEYS[1], 120)
        return allowed
        """
        allowed = await client.eval(script, 1, key, now, self._rate, refill_per_s)
        return bool(allowed)

    def _allow_local(self, ip: str) -> bool:
        now = time.monotonic()
        refill_per_s = self._rate / 60.0
        tokens, ts = self._local.get(ip, (float(self._rate), now))
        tokens = min(self._rate, tokens + (now - ts) * refill_per_s)
        if tokens >= 1:
            self._local[ip] = (tokens - 1, now)
            return True
        self._local[ip] = (tokens, now)
        return False


def _instrument_otel(app: FastAPI) -> None:
    """Best-effort OTel auto-instrumentation (F9): turns the exporter wiring in
    observability.py into actual spans. No-op (warns) when the instrumentation
    extras aren't installed, so the base toolkit stays dependency-light."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:  # pragma: no cover - optional extra
        log.warning("server.otel_instrumentation_missing")
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics.json")
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except ImportError:  # pragma: no cover - optional extra
        pass
    log.info("server.otel_instrumented")


def create_app(
    settings: BaseAgentSettings,
    *,
    title: str | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    metrics_public: bool = False,
) -> FastAPI:
    """Build the shared agent FastAPI app."""
    setup_observability(settings)
    metrics = MetricsRegistry(settings.agent_name)

    # Fail-closed: outside dev, a missing JWT secret silently opens /metrics.json
    # and drops upstream auth (AF-04). Refuse to construct the app rather than
    # boot a prod/staging server with auth disabled and no operator signal.
    if settings.env != "dev" and not settings.jwt_secret and not metrics_public:
        raise RuntimeError(
            "JWT_SECRET is empty but ENV is not 'dev': refusing to start with "
            "open /metrics.json and unauthenticated upstream calls. Set JWT_SECRET "
            "(or ENV=dev for local use)."
        )
    if not settings.jwt_secret and metrics_public:
        log.warning(
            "server.metrics_public_open",
            agent=settings.agent_name,
            env=settings.env,
            detail="/metrics.json is served WITHOUT authentication",
        )

    # Egress boot gate (BR-011): fail-closed if private_mode is on in
    # non-dev with a placeholder gateway URL.
    settings.validate_egress_boot_gate()

    # Readiness flag: lifespan flips this False on shutdown so /readyz returns 503
    # before the pool/scheduler tear down, letting the proxy drain in flight (F8).
    ready_state = {"accepting": True}

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("server.startup", agent=settings.agent_name, env=settings.env, port=settings.port)
        try:
            if lifespan is not None:
                async with lifespan(app):
                    yield
            else:
                yield
        finally:
            # Flip readiness first so /readyz -> 503 and the proxy stops routing,
            # then the wrapped lifespan's own teardown (pools/scheduler) runs.
            ready_state["accepting"] = False
            log.info("server.shutdown", agent=settings.agent_name)

    # Gate interactive docs + schema in prod: the full route map (incl. admin
    # endpoints) should not be publicly browsable there (F7). Kept on in dev.
    # ``None`` disables the route; in dev FastAPI's defaults ("/docs" etc.) apply.
    _prod = settings.env == "prod"
    app = FastAPI(
        title=title or settings.agent_name,
        version="0.1.0",
        lifespan=_lifespan,
        docs_url=None if _prod else "/docs",
        redoc_url=None if _prod else "/redoc",
        openapi_url=None if _prod else "/openapi.json",
    )
    # Stash the in-process metrics registry so routers/lifespans can register
    # custom metrics, a last_run provider, or the LLM usage object.
    app.state.metrics = metrics
    # Stash settings so routers/lifespans can resolve config at request time.
    app.state.settings = settings

    class _MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            # Per-request correlation: accept an inbound X-Request-ID or mint one,
            # bind it (+ env/agent already bound at startup) into structlog
            # contextvars for the request scope, and echo it back (F13).
            request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
            tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
            counted = request.url.path not in _UNCOUNTED_PATHS
            if counted:
                metrics.record_request()
            try:
                response = await call_next(request)
            except Exception:
                # Unhandled exception -> counts as an error, then re-raise so the
                # registered exception handler still produces the 500 response.
                if counted:
                    metrics.record_error()
                raise
            finally:
                structlog.contextvars.reset_contextvars(**tokens)
            if counted and response.status_code >= 500:
                metrics.record_error()
            response.headers["X-Request-ID"] = request_id
            return response

    app.add_middleware(_MetricsMiddleware)

    class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            response = await call_next(request)
            if settings.env in ("staging", "prod"):
                response.headers.setdefault(
                    "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
                )
                response.headers.setdefault(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
                    "form-action 'self'",
                )
                response.headers.setdefault("X-Frame-Options", "DENY")
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
                response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                response.headers.setdefault(
                    "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
                )
            return response

    app.add_middleware(_SecurityHeadersMiddleware)

    # Per-IP rate limit on non-ops routes (webhooks, admin POSTs). Opt-in via
    # rate_limit_per_minute; Redis-backed across replicas, local fallback (ARCH-009).
    if settings.rate_limit_per_minute > 0:
        limiter = _TokenBucketLimiter(settings)
        app.state.rate_limiter = limiter

        class _RateLimitMiddleware(BaseHTTPMiddleware):
            async def dispatch(
                self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
            ) -> Response:
                if request.url.path in _RATE_LIMIT_EXEMPT_PATHS:
                    return await call_next(request)
                ip = _client_ip(request, trust_proxy=settings.trust_proxy)
                if not await limiter.allow(ip):
                    log.warning("server.rate_limited", ip=ip, path=request.url.path)
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "rate limit exceeded"},
                        headers={"Retry-After": "60"},
                    )
                return await call_next(request)

        app.add_middleware(_RateLimitMiddleware)
        # Separate stricter limiter for LLM inference paths (ARCH-009).
        llm_limiter = _TokenBucketLimiter(settings)

        class _LLMRateLimitMiddleware(BaseHTTPMiddleware):
            async def dispatch(
                self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
            ) -> Response:
                if not request.url.path.startswith("/llm/"):
                    return await call_next(request)
                ip = _client_ip(request, trust_proxy=settings.trust_proxy)
                if not await llm_limiter.allow(ip):
                    log.warning("server.llm_rate_limited", ip=ip, path=request.url.path)
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "LLM inference rate limit exceeded"},
                        headers={"Retry-After": "10"},
                    )
                return await call_next(request)

        app.add_middleware(_LLMRateLimitMiddleware)

    # Host-header allowlist (closed by default; proxy enforces when empty).
    if settings.trusted_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    # CORS allowlist (empty => no middleware => same-origin only, the safe default).
    if settings.cors_allow_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Auto-instrument for OTel when the exporter endpoint is configured (F9).
    if settings.otel_exporter_otlp_endpoint:
        _instrument_otel(app)

    def _metrics_guard(authorization: str = Header(default="")) -> None:
        # Admin-gated when a JWT secret is configured (Plan 4 §3.2). Open only if
        # explicitly made public or if no secret is set (local/dev), so the
        # already-deployed monitoring agent keeps working unchanged.
        if metrics_public or not settings.jwt_secret:
            return
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            from agentkit.auth import AuthError

            raise AuthError("missing bearer token")
        principal = verify_token(
            token,
            secret=settings.jwt_secret,
            algorithms=settings.jwt_algorithms,
            audience=settings.jwt_audience,
        )
        if not principal.is_admin:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")

    @app.get("/metrics.json", tags=["ops"])
    async def metrics_json(_: None = Depends(_metrics_guard)) -> JSONResponse:
        # Uniform observability surface consumed by the dashboard + meta-monitor.
        return JSONResponse(content=await metrics.snapshot())

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        # Liveness: process is up. Cheap, no I/O.
        return {"status": "ok", "agent": settings.agent_name}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> JSONResponse:
        # Readiness: dependencies reachable. Used by proxy/orchestrator before routing.
        # During shutdown drain we report not-ready first (F8) so the proxy stops
        # routing before pools/scheduler tear down — skip the DB ping in that case.
        if not ready_state["accepting"]:
            return JSONResponse(
                status_code=503,
                content={"ready": False, "checks": {}, "draining": True},
            )
        checks = await db.ping(settings)
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "checks": checks},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("server.unhandled", path=request.url.path, error=str(exc), exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    return app
