"""HTTP surface for the meta-monitoring agent built on the shared FastAPI factory.

Endpoints (in addition to inherited /healthz, /readyz):
- POST /webhook/sentry  — Sentry issue-alert webhook intake.
- POST /webhook/alert   — generic Alertmanager-style webhook intake.
- GET  /incidents       — recent triaged incidents (admin-only).
- POST /sweep           — trigger a manual health sweep (admin-only).

The APScheduler health sweep and the shared Postgres pool are wired in the
lifespan. Webhook intake builds Signals via collectors and runs them through
the triage graph; persistence + dedup + alerting all happen inside the graph.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from agentkit import LLMClient, create_app, get_logger
from agentkit.auth import (
    Principal,
    make_principal_dependency,
    require_admin,
    verify_webhook_signature,
)
from agentkit.db import pg_pool
from fastapi import Depends, FastAPI, HTTPException, Request
from psycopg_pool import AsyncConnectionPool

from .collectors import (
    collect_container_states,
    collect_heartbeats,
    heartbeat_to_signal,
    parse_alert,
    parse_sentry,
    probe_agent_endpoint,
)
from .graph import build_graph
from .scheduler import MetaDeps, build_scheduler, run_meta_sweep, run_sweep
from .settings import get_settings
from .store import IncidentStore, ProbeStateStore

log = get_logger("monitoring_agent.api")

settings = get_settings()
_principal = make_principal_dependency(settings)


def admin_principal(principal: Principal = Depends(_principal)) -> Principal:
    return require_admin(principal)


# Provider signature headers checked on the webhook routes. Sentry sends
# ``Sentry-Hook-Signature``; the generic Alertmanager route accepts a
# deployment-configured ``X-Webhook-Signature``. Both carry HMAC-SHA256 of the
# raw body keyed by ``settings.webhook_secret``.
_SENTRY_SIG_HEADER = "Sentry-Hook-Signature"
_GENERIC_SIG_HEADER = "X-Webhook-Signature"


async def _verified_webhook_body(request: Request, sig_header: str) -> bytes:
    """Read the raw webhook body once, enforcing a size cap then an HMAC check.

    Fail-closed (ARCH-001 / AF-01): with no ``webhook_secret`` configured every
    request is rejected. Oversized bodies are refused before hashing (AF-08).
    Returns the raw bytes so the caller parses them without a second read.
    """
    raw = await request.body()
    if len(raw) > settings.webhook_max_body_bytes:
        raise HTTPException(status_code=413, detail="webhook body too large")
    signature = request.headers.get(sig_header)
    if not verify_webhook_signature(raw, signature, settings.webhook_secret):
        log.warning("webhook.rejected", reason="bad_signature", path=request.url.path)
        raise HTTPException(status_code=401, detail="invalid or missing webhook signature")
    return raw


def _db_url_for(db_name: str) -> str:
    """The monitor's own libpq URL with the database segment swapped.

    Used for cross-DB heartbeat reads (Plan 2 §1.4): the monitor holds the shared
    ``appuser`` creds and each agent has its own database, so it reconnects to that
    DB read-only. Same host/user/password, different database.
    """
    base = settings.database_url
    head, _, _ = base.rpartition("/")
    return f"{head}/{db_name}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with pg_pool(settings) as pool:
        store = await IncidentStore.create(pool)
        probe_state = await ProbeStateStore.create(pool)
        llm = LLMClient.from_settings(settings)
        graph = build_graph(
            llm,
            store,
            rabbitmq_url=settings.rabbitmq_url,
            agent_name=settings.agent_name,
            dedup_window_minutes=settings.dedup_window_minutes,
            brand=settings.brand,
            notify_max_attempts=settings.notify_max_attempts,
            notify_retry_backoff_seconds=settings.notify_retry_backoff_seconds,
        )

        # Lazily-opened, cached per-DB pools for cross-DB heartbeat reads.
        cross_db_pools: dict[str, AsyncConnectionPool] = {}

        async def pool_for(db_name: str) -> AsyncConnectionPool:
            existing = cross_db_pools.get(db_name)
            if existing is not None:
                return existing
            p = AsyncConnectionPool(
                conninfo=_db_url_for(db_name),
                min_size=0,
                max_size=2,
                open=False,
                # Enforce the documented read-only guarantee (F18): the monitor
                # holds write-capable ``appuser`` creds but must only ever SELECT
                # against a sibling agent's DB. ``default_transaction_read_only``
                # makes any stray write to a cross-DB pool fail server-side.
                kwargs={
                    "autocommit": True,
                    "options": "-c default_transaction_read_only=on",
                },
            )
            await p.open(wait=True)
            cross_db_pools[db_name] = p
            return p

        meta_deps = MetaDeps(probe_state=probe_state, pool_for=pool_for)

        app.state.pool = pool
        app.state.llm = llm
        app.state.store = store
        app.state.probe_state = probe_state
        app.state.graph = graph
        app.state.meta_deps = meta_deps
        app.state.pool_for = pool_for

        scheduler = build_scheduler(settings, graph, meta_deps, store=store)
        scheduler.start()
        app.state.scheduler = scheduler
        log.info(
            "api.started",
            targets=settings.targets,
            interval=settings.poll_interval_seconds,
            meta_enabled=settings.meta_enabled,
            meta_interval=settings.meta_poll_interval_seconds,
            agents=list(settings.agent_endpoints),
        )
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            for p in cross_db_pools.values():
                await p.close()


# create_app is an untyped agentkit factory (returns Any); pin the result to
# FastAPI so the route decorators below are seen as typed under mypy --strict.
app: FastAPI = create_app(settings, title="monitoring-agent", lifespan=lifespan)


def _parse_json_body(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc


@app.post("/webhook/sentry")
async def webhook_sentry(request: Request) -> dict[str, Any]:
    """Intake a Sentry issue-alert webhook and triage it. HMAC-gated."""
    raw = await _verified_webhook_body(request, _SENTRY_SIG_HEADER)
    payload = _parse_json_body(raw)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    signal = parse_sentry(payload)
    result = await app.state.graph.ainvoke({"signal": signal})
    return _intake_response(result)


@app.post("/webhook/alert")
async def webhook_alert(request: Request) -> dict[str, Any]:
    """Intake a generic Alertmanager-style webhook (one or many alerts). HMAC-gated."""
    raw = await _verified_webhook_body(request, _GENERIC_SIG_HEADER)
    payload = _parse_json_body(raw)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    signals = parse_alert(payload)
    # Cap the number of LLM-triaged alerts from one webhook (AF-08); extras are
    # dropped rather than fanned out into unbounded classify() calls.
    dropped = max(0, len(signals) - settings.webhook_max_alerts)
    if dropped:
        log.warning("webhook.alerts_truncated", received=len(signals), dropped=dropped)
        signals = signals[: settings.webhook_max_alerts]
    results = [await app.state.graph.ainvoke({"signal": s}) for s in signals]
    return {
        "received": len(signals),
        "dropped": dropped,
        "alerted": sum(1 for r in results if r.get("alerted")),
        "incidents": [_intake_response(r) for r in results],
    }


@app.get("/incidents")
async def list_incidents(
    limit: int = 50,
    _: Principal = Depends(admin_principal),
) -> dict[str, Any]:
    """Recent triaged incidents, newest first. Admin-only."""
    limit = max(1, min(limit, 500))
    incidents = await app.state.store.recent(limit=limit)
    return {
        "count": len(incidents),
        "incidents": [
            {
                "id": i.id,
                "dedup_key": i.dedup_key,
                "source": i.source,
                "severity": i.severity,
                "title": i.title,
                "count": i.count,
                "status": i.status,
                "first_seen": i.first_seen.isoformat(),
                "last_seen": i.last_seen.isoformat(),
            }
            for i in incidents
        ],
    }


@app.post("/sweep")
async def trigger_sweep(_: Principal = Depends(admin_principal)) -> dict[str, Any]:
    """Run a health sweep on demand. Admin-only."""
    return await run_sweep(settings, app.state.graph)


@app.post("/meta-sweep")
async def trigger_meta_sweep(_: Principal = Depends(admin_principal)) -> dict[str, Any]:
    """Run a meta-monitoring sweep (agents + infra + host) on demand. Admin-only."""
    return await run_meta_sweep(settings, app.state.graph, app.state.meta_deps)


@app.get("/agents")
async def agents_snapshot(_: Principal = Depends(admin_principal)) -> dict[str, Any]:
    """Current agents health snapshot — the data the dashboard reads. Admin-only.

    Live one-shot probe (no streak/alert side-effects): each agent's
    /healthz+/readyz, watched container states, last batch heartbeat, plus the
    open agent_health incident count. Read-only; never the raw docker socket.
    """
    timeout = settings.meta_probe_timeout_seconds
    agents: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for name, base_url in settings.agent_endpoints.items():
            r = await probe_agent_endpoint(name, base_url, client=client, timeout=timeout)
            entry: dict[str, Any] = {
                "agent": name,
                "url": base_url,
                "healthz_ok": r.healthz_ok,
                "ready": r.ready,
                "failed_checks": r.failed_checks,
                "latency_seconds": r.latency_seconds,
                "error": r.error,
            }
            agents.append(entry)
        try:
            states = await collect_container_states(
                settings.docker_proxy_url,
                settings.watched_containers,
                client=client,
                timeout=timeout,
            )
        except Exception as exc:
            log.warning("agents.containers_failed", error=str(exc))
            states = []

    containers = [
        {
            "name": s.name,
            "status": s.status,
            "oom_killed": s.oom_killed,
            "restart_count": s.restart_count,
            "health": s.health,
            "exit_code": s.exit_code,
        }
        for s in states
    ]

    heartbeats: list[dict[str, Any]] = []
    try:
        hbs = await collect_heartbeats(app.state.pool_for, settings.heartbeat_sources)
        for hb in hbs:
            sig = heartbeat_to_signal(hb)
            heartbeats.append(
                {
                    "agent": hb.agent,
                    "last_finished": hb.last_finished.isoformat() if hb.last_finished else None,
                    "last_status": hb.last_status,
                    "expected_interval_s": hb.expected_interval_s,
                    "overdue_or_failed": sig is not None,
                }
            )
    except Exception as exc:
        log.warning("agents.heartbeats_failed", error=str(exc))

    open_incidents = await app.state.store.open_count_by_source("agent_health")

    return {
        "agents": agents,
        "containers": containers,
        "heartbeats": heartbeats,
        "open_agent_health_incidents": open_incidents,
    }


def _intake_response(result: dict[str, Any]) -> dict[str, Any]:
    incident = result.get("incident")
    return {
        "incident_id": getattr(incident, "id", None),
        "severity": result.get("severity"),
        "category": result.get("category"),
        "action": result.get("action"),
        "suppressed": result.get("suppressed", False),
        "alerted": result.get("alerted", False),
        "count": getattr(incident, "count", None),
    }
