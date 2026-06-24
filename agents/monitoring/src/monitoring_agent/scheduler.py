"""APScheduler wiring for the periodic health sweeps.

Two jobs share one scheduler:

- ``health_sweep`` — the original prod HTTP probe of the public service surface,
  every ``poll_interval_seconds``.
- ``meta_sweep`` — meta-monitoring (Plan 2): scrape the agents + infra +
  host, build ``agent_health`` Signals, push each through the SAME triage graph,
  every ``meta_poll_interval_seconds``.

Both coroutines also back the manual ``POST /sweep`` / ``POST /meta-sweep``
endpoints so on-demand and scheduled behaviour are identical.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from agentkit import get_logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from langgraph.graph.state import CompiledStateGraph

from .collectors import (
    collect_container_states,
    collect_heartbeats,
    collect_queue_depths,
    container_to_signals,
    endpoint_to_signals,
    heartbeat_to_signal,
    host_vitals_to_signals,
    probe_agent_endpoint,
    queue_to_signal,
    read_host_meminfo,
    sweep,
)
from .settings import Settings
from .store import IncidentStore, ProbeStateStore

if TYPE_CHECKING:
    from .collectors import Signal

log = get_logger("monitoring_agent.scheduler")


@dataclass
class MetaDeps:
    """Dependencies the meta sweep needs beyond settings + graph.

    ``probe_state`` carries cross-sweep streak/delta memory; ``pool_for`` returns
    a per-DB connection pool for cross-DB heartbeat reads (None disables the
    heartbeat collector, e.g. in a minimal deployment).
    """

    probe_state: ProbeStateStore
    pool_for: Callable[[str], Awaitable[Any]] | None = None


async def run_sweep(
    settings: Settings,
    graph: CompiledStateGraph[Any, Any, Any, Any],
) -> dict[str, Any]:
    """Probe targets and triage any unhealthy ones. Returns a small summary."""
    signals = await sweep(
        settings.targets,
        slow_threshold_seconds=settings.slow_threshold_seconds,
        cert_warn_days=settings.cert_warn_days,
        timeout=settings.probe_timeout_seconds,
        concurrency=settings.sweep_concurrency,
    )
    triaged = 0
    alerted = 0
    for sig in signals:
        result = await graph.ainvoke({"signal": sig})
        triaged += 1
        if result.get("alerted"):
            alerted += 1
    log.info(
        "scheduler.sweep_done",
        targets=len(settings.targets),
        problems=len(signals),
        triaged=triaged,
        alerted=alerted,
    )
    return {"targets": len(settings.targets), "problems": len(signals), "alerted": alerted}


async def _collect_meta_signals(settings: Settings, deps: MetaDeps) -> list[Signal]:
    """Gather every agent_health Signal for one meta sweep.

    Collector failures are isolated: any one source raising is logged and its
    Signals are simply absent that sweep — the monitor must never crash on a
    probe (especially when watching things that are themselves broken).
    """
    # ``Signal`` is imported lazily under TYPE_CHECKING (top of module) to avoid a
    # cycle at import time; only the annotation below needs it, and with
    # ``from __future__ import annotations`` that annotation is never evaluated.
    signals: list[Signal] = []
    timeout = settings.meta_probe_timeout_seconds

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        # 1.1 — agent /healthz + /readyz liveness/readiness.
        for agent, base_url in settings.agent_endpoints.items():
            try:
                res = await probe_agent_endpoint(agent, base_url, client=client, timeout=timeout)
                streak = await deps.probe_state.record_endpoint(
                    f"endpoint:{agent}", ok=res.healthz_ok
                )
                signals.extend(
                    endpoint_to_signals(
                        res,
                        slow_threshold_s=settings.slow_threshold_seconds,
                        fail_streak=streak,
                        fail_streak_to_alert=settings.fail_streak_to_alert,
                    )
                )
            except Exception as exc:
                log.warning("meta.endpoint_failed", agent=agent, error=str(exc))

        # 1.2 — docker container state (via read-only proxy).
        try:
            states = await collect_container_states(
                settings.docker_proxy_url,
                settings.watched_containers,
                client=client,
                timeout=timeout,
            )
            for st in states:
                prev = await deps.probe_state.record_restart(
                    f"container:{st.name}", st.restart_count
                )
                signals.extend(
                    container_to_signals(
                        prev, st, restart_loop_threshold=settings.restart_loop_threshold
                    )
                )
        except Exception as exc:
            log.warning("meta.containers_failed", error=str(exc))

        # 1.5 — RabbitMQ unconsumed queue depth (alert-delivery canary).
        try:
            depths = await collect_queue_depths(
                settings.rabbit_mgmt_url,
                settings.watched_queues,
                client=client,
                timeout=timeout,
            )
            for name, depth in depths.items():
                prev_depth = await deps.probe_state.record_depth(f"queue:{name}", depth)
                sig = queue_to_signal(
                    name, depth, prev_depth, threshold=settings.queue_depth_threshold
                )
                if sig is not None:
                    signals.append(sig)
        except Exception as exc:
            log.warning("meta.queues_failed", error=str(exc))

    # 1.4 — cross-DB batch/cron heartbeat freshness.
    if deps.pool_for is not None and settings.heartbeat_sources:
        try:
            hbs = await collect_heartbeats(deps.pool_for, settings.heartbeat_sources)
            for hb in hbs:
                sig = heartbeat_to_signal(hb)
                if sig is not None:
                    signals.append(sig)
        except Exception as exc:
            log.warning("meta.heartbeats_failed", error=str(exc))

    # Host vitals — the RAM budget is the host's real constraint.
    try:
        meminfo = read_host_meminfo(settings.host_meminfo_path)
        if meminfo:
            signals.extend(
                host_vitals_to_signals(
                    meminfo,
                    mem_avail_warn_mb=settings.host_mem_avail_warn_mb,
                    swap_used_warn_mb=settings.host_swap_used_warn_mb,
                )
            )
    except Exception as exc:
        log.warning("meta.host_vitals_failed", error=str(exc))

    return signals


async def run_meta_sweep(
    settings: Settings,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    deps: MetaDeps,
) -> dict[str, Any]:
    """Scrape the agents + infra + host and triage any agent_health problems."""
    signals = await _collect_meta_signals(settings, deps)
    triaged = 0
    alerted = 0
    for sig in signals:
        result = await graph.ainvoke({"signal": sig})
        triaged += 1
        if result.get("alerted"):
            alerted += 1
    log.info(
        "scheduler.meta_sweep_done",
        agents=len(settings.agent_endpoints),
        containers=len(settings.watched_containers),
        problems=len(signals),
        triaged=triaged,
        alerted=alerted,
    )
    return {
        "agents": len(settings.agent_endpoints),
        "containers": len(settings.watched_containers),
        "problems": len(signals),
        "alerted": alerted,
    }


async def run_retention_prune(
    settings: Settings,
    store: IncidentStore,
    probe_state: ProbeStateStore,
) -> dict[str, int]:
    """Prune the unbounded time-series tables; return per-table delete counts.

    Bounds otherwise-forever growth (incidents history + probe-state rows). Each
    prune is isolated so one failing does not block the other — the monitor must
    never crash on housekeeping.
    """
    incidents_pruned = 0
    probe_state_pruned = 0
    try:
        incidents_pruned = await store.prune(retention_days=settings.incident_retention_days)
    except Exception as exc:
        log.warning("retention.incidents_prune_failed", error=str(exc))
    try:
        probe_state_pruned = await probe_state.prune(
            retention_days=settings.probe_state_retention_days
        )
    except Exception as exc:
        log.warning("retention.probe_state_prune_failed", error=str(exc))
    log.info(
        "scheduler.retention_prune_done",
        incidents_pruned=incidents_pruned,
        probe_state_pruned=probe_state_pruned,
    )
    return {"incidents_pruned": incidents_pruned, "probe_state_pruned": probe_state_pruned}


def build_scheduler(
    settings: Settings,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    meta_deps: MetaDeps | None = None,
    store: IncidentStore | None = None,
) -> AsyncIOScheduler:
    """Create (but do not start) the AsyncIOScheduler with the sweep jobs.

    The ``meta_sweep`` job is only registered when meta-monitoring is enabled and
    ``meta_deps`` (probe-state store, optional per-DB pool factory) is supplied.
    The ``retention_prune`` job is registered when both ``store`` and ``meta_deps``
    are supplied and the prune cadence is enabled.
    """
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_sweep,
        trigger="interval",
        seconds=settings.poll_interval_seconds,
        args=[settings, graph],
        id="health_sweep",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    if settings.meta_enabled and meta_deps is not None:
        scheduler.add_job(
            run_meta_sweep,
            trigger="interval",
            seconds=settings.meta_poll_interval_seconds,
            args=[settings, graph, meta_deps],
            id="meta_sweep",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    # Retention prune: needs both stores. probe_state rides on meta_deps; the
    # incidents store is passed explicitly. Skip if either is unavailable or the
    # cadence is disabled.
    if (
        store is not None
        and meta_deps is not None
        and settings.retention_prune_interval_seconds > 0
    ):
        scheduler.add_job(
            run_retention_prune,
            trigger="interval",
            seconds=settings.retention_prune_interval_seconds,
            args=[settings, store, meta_deps.probe_state],
            id="retention_prune",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    return scheduler
