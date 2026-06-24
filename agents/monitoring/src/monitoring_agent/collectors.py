"""Signal intake: HTTP health probe + webhook payload parsers.

Everything here produces a normalized :class:`Signal` — a provider-agnostic
description of one observed event. The triage graph never sees raw Sentry /
Alertmanager JSON; it only sees Signals, so adding a new source means adding a
parser here and nothing downstream changes.

A Signal carries a ``fingerprint``: a stable identity for the *thing* that is
wrong (not the individual occurrence). Dedup keys are derived from
``source + fingerprint`` so the same recurring failure collapses into one
incident.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from agentkit import get_logger

log = get_logger("monitoring_agent.collectors")


@dataclass
class Signal:
    """A normalized observation handed to the triage graph."""

    source: str  # "sentry" | "healthcheck" | "alert"
    fingerprint: str  # stable identity of the failing thing
    title: str
    body: str
    # Hint severity from the source; classify() may override.
    suggested_severity: str = "warning"  # info | warning | critical
    url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Sentry issue-alert webhook
# --------------------------------------------------------------------------- #

# Sentry "level" -> our severity buckets.
_SENTRY_LEVEL_MAP = {
    "fatal": "critical",
    "error": "warning",
    "warning": "warning",
    "info": "info",
    "debug": "info",
}


def parse_sentry(payload: dict[str, Any]) -> Signal:
    """Parse a Sentry issue-alert webhook payload into a Signal.

    Sentry's payload shape has shifted over versions; we read defensively from
    the common locations (``data.event`` / ``data.issue`` / top-level).
    """
    data = payload.get("data", payload)
    event = data.get("event", data) if isinstance(data, dict) else {}
    issue = data.get("issue", {}) if isinstance(data, dict) else {}

    title = (
        event.get("title")
        or issue.get("title")
        or event.get("message")
        or payload.get("message")
        or "Sentry event"
    )
    culprit = event.get("culprit") or issue.get("culprit") or ""
    level = (event.get("level") or issue.get("level") or "error").lower()
    project = (
        event.get("project")
        or payload.get("project")
        or (data.get("project_slug") if isinstance(data, dict) else None)
        or "unknown"
    )
    permalink = (
        event.get("web_url")
        or event.get("issue_url")
        or issue.get("permalink")
        or payload.get("url")
    )
    # Occurrence count if Sentry includes it.
    count = _to_int(event.get("count") or issue.get("count") or payload.get("count"))

    # Sentry already supplies a grouping fingerprint sometimes; prefer it,
    # else fall back to project+culprit+title for a stable identity.
    fp_parts = event.get("fingerprint")
    if isinstance(fp_parts, list) and fp_parts:
        fingerprint = ":".join(str(p) for p in fp_parts)
    else:
        fingerprint = f"{project}:{culprit or title}"

    body_lines = [
        f"culprit: {culprit}" if culprit else "",
        f"project: {project}",
        f"level: {level}",
    ]
    if count:
        body_lines.append(f"count: {count}")
    body = "\n".join(line for line in body_lines if line)

    return Signal(
        source="sentry",
        fingerprint=fingerprint,
        title=str(title),
        body=body,
        suggested_severity=_SENTRY_LEVEL_MAP.get(level, "warning"),
        url=str(permalink) if permalink else None,
        meta={"project": str(project), "culprit": culprit, "level": level, "count": count},
    )


# --------------------------------------------------------------------------- #
# Generic Alertmanager-style webhook
# --------------------------------------------------------------------------- #

# Alertmanager severity label -> our buckets.
_ALERT_SEVERITY_MAP = {
    "critical": "critical",
    "page": "critical",
    "error": "warning",
    "warning": "warning",
    "info": "info",
    "none": "info",
}


def parse_alert(payload: dict[str, Any]) -> list[Signal]:
    """Parse an Alertmanager-style payload (one or many alerts) into Signals.

    Accepts both the Alertmanager v4 shape (``{"alerts": [...]}``) and a single
    flat alert object.
    """
    raw_alerts = payload.get("alerts")
    if not isinstance(raw_alerts, list):
        raw_alerts = [payload]

    signals: list[Signal] = []
    for a in raw_alerts:
        if not isinstance(a, dict):
            continue
        labels = a.get("labels", {}) if isinstance(a.get("labels"), dict) else {}
        annotations = a.get("annotations", {}) if isinstance(a.get("annotations"), dict) else {}

        name = labels.get("alertname") or a.get("name") or "alert"
        instance = labels.get("instance") or labels.get("service") or ""
        sev_label = str(labels.get("severity") or a.get("severity") or "warning").lower()
        status = a.get("status", "firing")

        title = annotations.get("summary") or name
        body = annotations.get("description") or annotations.get("message") or ""
        if instance:
            body = (body + f"\ninstance: {instance}").strip()
        body = (body + f"\nstatus: {status}").strip()

        # Alertmanager ships a per-alert fingerprint; else derive from labels.
        fingerprint = a.get("fingerprint") or f"{name}:{instance}"

        signals.append(
            Signal(
                source="alert",
                fingerprint=str(fingerprint),
                title=str(title),
                body=body,
                suggested_severity=_ALERT_SEVERITY_MAP.get(sev_label, "warning"),
                url=a.get("generatorURL") or annotations.get("runbook_url"),
                meta={"alertname": name, "instance": instance, "status": status, "labels": labels},
            )
        )
    return signals


# --------------------------------------------------------------------------- #
# Scheduled HTTP health probe
# --------------------------------------------------------------------------- #


@dataclass
class ProbeResult:
    target: str
    ok: bool
    status_code: int | None
    latency_seconds: float | None
    tls_expiry_days: int | None
    error: str | None = None


# SSRF hygiene (AF-07): only http(s) URLs are probable. These targets/endpoints
# are operator-set but env/JSON-overridable, so a stray ``file://`` /
# ``gopher://`` scheme must never reach httpx.
_ALLOWED_URL_SCHEMES = {"http", "https"}


def is_probeable_url(url: str) -> bool:
    """True iff ``url`` parses to an allowlisted scheme with a host.

    Rejects non-http(s) schemes and host-less URLs before any network call so a
    config-influenced target can't redirect the probe at an internal resource or
    a local file/metadata endpoint.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in _ALLOWED_URL_SCHEMES and bool(parsed.hostname)


async def probe_target(
    target: str,
    *,
    timeout: float = 10.0,  # noqa: ASYNC109 - configurable per-probe timeout is the API
    client: httpx.AsyncClient | None = None,
) -> ProbeResult:
    """HTTP GET a target; capture status, latency and TLS expiry days.

    Redirects are NOT followed (AF-07): a redirect is itself a signal, and
    chasing it would let a probed target bounce the request into the internal
    network. Non-http(s) targets are rejected before any connection.
    """
    if not is_probeable_url(target):
        return ProbeResult(
            target=target,
            ok=False,
            status_code=None,
            latency_seconds=None,
            tls_expiry_days=None,
            error="rejected: target URL is not an http(s) URL",
        )
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        start = time.perf_counter()
        try:
            resp = await client.get(target)
            latency = time.perf_counter() - start
            status_code: int | None = resp.status_code
            ok = resp.status_code < 500
            error: str | None = None
        except httpx.HTTPError as exc:
            latency = time.perf_counter() - start
            status_code = None
            ok = False
            error = f"{type(exc).__name__}: {exc}"

        tls_days = await _tls_expiry_days(target, timeout=timeout)
        return ProbeResult(
            target=target,
            ok=ok,
            status_code=status_code,
            latency_seconds=round(latency, 4),
            tls_expiry_days=tls_days,
            error=error,
        )
    finally:
        if owns_client:
            await client.aclose()


async def _tls_expiry_days(target: str, *, timeout: float) -> int | None:  # noqa: ASYNC109 - configurable per-probe timeout is the API
    """Days until the target's TLS cert expires. None for non-https / errors."""
    parsed = urlparse(target)
    if parsed.scheme != "https":
        return None
    host = parsed.hostname
    port = parsed.port or 443
    if not host:
        return None

    def _check() -> int | None:
        ctx = ssl.create_default_context()
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            ctx.wrap_socket(sock, server_hostname=host) as ssock,
        ):
            cert = ssock.getpeercert()
        not_after = cert.get("notAfter") if cert else None
        if not not_after or not isinstance(not_after, str):
            return None
        expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        return (expiry - datetime.now(UTC)).days

    try:
        return await asyncio.to_thread(_check)
    except Exception as exc:
        log.warning("collectors.tls_check_failed", target=target, error=str(exc))
        return None


def probe_to_signal(
    res: ProbeResult,
    *,
    slow_threshold_seconds: float,
    cert_warn_days: int,
) -> Signal | None:
    """Turn a probe result into a Signal, or None if the target is healthy.

    Picks the worst observed condition: down > slow > cert-expiring.
    """
    host = urlparse(res.target).hostname or res.target

    if not res.ok:
        detail = res.error or f"HTTP {res.status_code}"
        return Signal(
            source="healthcheck",
            fingerprint=f"down:{res.target}",
            title=f"{host} is DOWN",
            body=f"target: {res.target}\nstatus: {res.status_code}\nerror: {detail}",
            suggested_severity="critical",
            url=res.target,
            meta={"kind": "down", "status_code": res.status_code, "error": res.error},
        )

    if res.latency_seconds is not None and res.latency_seconds > slow_threshold_seconds:
        return Signal(
            source="healthcheck",
            fingerprint=f"slow:{res.target}",
            title=f"{host} is SLOW",
            body=(
                f"target: {res.target}\nlatency: {res.latency_seconds}s "
                f"(threshold {slow_threshold_seconds}s)\nstatus: {res.status_code}"
            ),
            suggested_severity="warning",
            url=res.target,
            meta={"kind": "slow", "latency_seconds": res.latency_seconds},
        )

    if res.tls_expiry_days is not None and res.tls_expiry_days <= cert_warn_days:
        sev = "critical" if res.tls_expiry_days <= 3 else "warning"
        return Signal(
            source="healthcheck",
            fingerprint=f"cert:{res.target}",
            title=f"{host} TLS cert expiring in {res.tls_expiry_days}d",
            body=f"target: {res.target}\ntls_expiry_days: {res.tls_expiry_days}",
            suggested_severity=sev,
            url=res.target,
            meta={"kind": "cert", "tls_expiry_days": res.tls_expiry_days},
        )

    return None


async def sweep(
    targets: list[str],
    *,
    slow_threshold_seconds: float,
    cert_warn_days: int,
    timeout: float = 10.0,  # noqa: ASYNC109 - configurable per-probe timeout is the API
    concurrency: int = 0,
) -> list[Signal]:
    """Probe every target concurrently; return Signals for unhealthy ones.

    ``concurrency`` caps how many probes run at once via a semaphore so a
    growing target list cannot open N sockets simultaneously; ``<= 0`` means
    unbounded (the historical behaviour). Result order matches ``targets``.
    """
    sem = asyncio.Semaphore(concurrency) if concurrency > 0 else None

    async def _bounded_probe(target: str, client: httpx.AsyncClient) -> ProbeResult:
        if sem is None:
            return await probe_target(target, timeout=timeout, client=client)
        async with sem:
            return await probe_target(target, timeout=timeout, client=client)

    # follow_redirects=False (AF-07): a redirect is a signal, not a hop to chase
    # into the internal network.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        results = await asyncio.gather(*(_bounded_probe(t, client) for t in targets))
    signals: list[Signal] = []
    for res in results:
        log.info(
            "collectors.probe",
            target=res.target,
            ok=res.ok,
            status_code=res.status_code,
            latency_seconds=res.latency_seconds,
            tls_expiry_days=res.tls_expiry_days,
        )
        sig = probe_to_signal(
            res,
            slow_threshold_seconds=slow_threshold_seconds,
            cert_warn_days=cert_warn_days,
        )
        if sig is not None:
            signals.append(sig)
    return signals


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Meta-monitoring: agents agent health (Plan 2)
# --------------------------------------------------------------------------- #
#
# The collectors below scrape the *agents itself* (siblings + shared infra)
# rather than the public service surface. Each one emits the SAME ``Signal``
# dataclass with ``source="agent_health"`` so the downstream graph
# (classify -> dedup -> decide -> notify) is reused verbatim. Fingerprints are
# stable per the failing thing (Plan 2 §1.1-§1.5) so dedup collapses recurrences.

AGENT_HEALTH_SOURCE = "agent_health"


@dataclass
class AgentEndpointResult:
    """Outcome of probing one agent's ``/healthz`` + ``/readyz`` loopback pair."""

    agent: str
    base_url: str
    healthz_ok: bool
    # None when /healthz already failed (we don't bother with /readyz then).
    ready: bool | None
    failed_checks: list[str]  # readyz checks reporting False, e.g. ["postgres"]
    latency_seconds: float | None
    error: str | None = None


@dataclass
class ContainerState:
    """A container's runtime state, read from the docker-socket-proxy."""

    name: str
    status: str  # running | restarting | exited | dead | created | ...
    oom_killed: bool
    restart_count: int
    health: str | None  # compose healthcheck: healthy | unhealthy | starting | None
    exit_code: int | None


@dataclass
class HeartbeatState:
    """Freshness of a batch/cron agent's last successful run (cross-DB read)."""

    agent: str
    last_finished: datetime | None
    last_status: str | None
    expected_interval_s: int  # 0 = on-demand (no overdue rule)


# --- HTTP liveness / readiness (Plan 2 §1.1) ------------------------------- #


async def probe_agent_endpoint(
    agent: str,
    base_url: str,
    *,
    client: httpx.AsyncClient,
    timeout: float = 5.0,  # noqa: ASYNC109 - configurable per-probe timeout is the API
) -> AgentEndpointResult:
    """GET ``<base_url>/healthz`` then ``/readyz``; parse the readiness body.

    Distinguishes *agent down* (``/healthz`` unreachable / failing) from *agent
    up but a dependency is broken* (``/healthz`` 200, ``/readyz`` 503 with a
    ``checks`` map) — they are different incidents with different remediation, so
    we never collapse them.
    """
    if not is_probeable_url(base_url):
        return AgentEndpointResult(
            agent=agent,
            base_url=base_url,
            healthz_ok=False,
            ready=None,
            failed_checks=[],
            latency_seconds=None,
            error="rejected: agent endpoint is not an http(s) URL",
        )
    base = base_url.rstrip("/")
    start = time.perf_counter()
    try:
        hz = await client.get(f"{base}/healthz", timeout=timeout)
        latency = time.perf_counter() - start
        healthz_ok = hz.status_code == 200
    except httpx.HTTPError as exc:
        return AgentEndpointResult(
            agent=agent,
            base_url=base_url,
            healthz_ok=False,
            ready=None,
            failed_checks=[],
            latency_seconds=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    if not healthz_ok:
        return AgentEndpointResult(
            agent=agent,
            base_url=base_url,
            healthz_ok=False,
            ready=None,
            failed_checks=[],
            latency_seconds=round(latency, 4),
            error=f"healthz HTTP {hz.status_code}",
        )

    ready: bool | None = None
    failed_checks: list[str] = []
    try:
        rz = await client.get(f"{base}/readyz", timeout=timeout)
        ready = rz.status_code == 200
        try:
            body = rz.json()
        except Exception:
            body = {}
        checks = body.get("checks") if isinstance(body, dict) else None
        if isinstance(checks, dict):
            failed_checks = sorted(k for k, v in checks.items() if v is False)
    except httpx.HTTPError as exc:
        ready = False
        failed_checks = []
        log.warning("collectors.readyz_failed", agent=agent, error=str(exc))

    return AgentEndpointResult(
        agent=agent,
        base_url=base_url,
        healthz_ok=True,
        ready=ready,
        failed_checks=failed_checks,
        latency_seconds=round(latency, 4),
    )


def endpoint_to_signals(
    r: AgentEndpointResult,
    *,
    slow_threshold_s: float,
    fail_streak: int,
    fail_streak_to_alert: int,
) -> list[Signal]:
    """Signals for one agent endpoint probe (Plan 2 §1.1, §3 streak rule).

    ``fail_streak`` is the consecutive-failure count for this target *including*
    the current probe (supplied by the caller from ``agent_probe_state``). A down
    agent only pages once the streak reaches ``fail_streak_to_alert`` so a single
    transient blip doesn't fan out.
    """
    signals: list[Signal] = []

    if not r.healthz_ok:
        if fail_streak >= fail_streak_to_alert:
            signals.append(
                Signal(
                    source=AGENT_HEALTH_SOURCE,
                    fingerprint=f"agent-down:{r.agent}",
                    title=f"agent {r.agent} is DOWN",
                    body=(
                        f"agent: {r.agent}\nurl: {r.base_url}\n"
                        f"error: {r.error}\nfail_streak: {fail_streak}"
                    ),
                    suggested_severity="critical",
                    url=r.base_url,
                    meta={"kind": "agent-down", "agent": r.agent, "fail_streak": fail_streak},
                )
            )
        return signals

    # Up but a dependency is unready -> distinct incident per failed check.
    if r.ready is False:
        checks = r.failed_checks or ["unknown"]
        for check in checks:
            signals.append(
                Signal(
                    source=AGENT_HEALTH_SOURCE,
                    fingerprint=f"agent-notready:{r.agent}:{check}",
                    title=f"agent {r.agent} not ready ({check})",
                    body=(
                        f"agent: {r.agent}\nurl: {r.base_url}\n"
                        f"failed_check: {check}\nfail_streak: {fail_streak}"
                    ),
                    suggested_severity="warning",
                    url=r.base_url,
                    meta={
                        "kind": "agent-notready",
                        "agent": r.agent,
                        "failed_check": check,
                        "fail_streak": fail_streak,
                    },
                )
            )
        return signals

    # Healthy + ready -> only a latency SLO breach is interesting.
    if r.latency_seconds is not None and r.latency_seconds > slow_threshold_s:
        signals.append(
            Signal(
                source=AGENT_HEALTH_SOURCE,
                fingerprint=f"agent-slow:{r.agent}",
                title=f"agent {r.agent} is SLOW",
                body=(
                    f"agent: {r.agent}\nurl: {r.base_url}\n"
                    f"latency: {r.latency_seconds}s (threshold {slow_threshold_s}s)"
                ),
                suggested_severity="warning",
                url=r.base_url,
                meta={"kind": "agent-slow", "agent": r.agent, "latency_seconds": r.latency_seconds},
            )
        )
    return signals


# --- Docker container state via the read-only socket-proxy (Plan 2 §1.2) --- #


async def collect_container_states(
    proxy_url: str,
    names: list[str],
    *,
    client: httpx.AsyncClient,
    timeout: float = 5.0,  # noqa: ASYNC109 - configurable per-probe timeout is the API
) -> list[ContainerState]:
    """Read container state for ``names`` via the docker-socket-proxy.

    Talks to the read-only proxy (``GET /containers/json?all=1`` then
    ``GET /containers/<id>/json``), never the raw socket (Plan 2 §2). Missing
    containers are returned with ``status="missing"`` so a vanished agent is
    visible rather than silently skipped.
    """
    base = proxy_url.rstrip("/")
    wanted = set(names)
    states: list[ContainerState] = []
    try:
        listing = await client.get(f"{base}/containers/json", params={"all": "1"}, timeout=timeout)
        listing.raise_for_status()
        containers = listing.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("collectors.docker_list_failed", error=str(exc))
        return states

    by_name: dict[str, str] = {}  # container name -> id
    for c in containers if isinstance(containers, list) else []:
        cid = c.get("Id")
        for raw in c.get("Names", []) or []:
            nm = raw.lstrip("/")
            if nm in wanted and cid:
                by_name[nm] = cid

    for name in names:
        cid = by_name.get(name)
        if cid is None:
            states.append(
                ContainerState(
                    name=name,
                    status="missing",
                    oom_killed=False,
                    restart_count=0,
                    health=None,
                    exit_code=None,
                )
            )
            continue
        try:
            insp = await client.get(f"{base}/containers/{cid}/json", timeout=timeout)
            insp.raise_for_status()
            states.append(_parse_container_inspect(name, insp.json()))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("collectors.docker_inspect_failed", container=name, error=str(exc))
            states.append(
                ContainerState(
                    name=name,
                    status="unknown",
                    oom_killed=False,
                    restart_count=0,
                    health=None,
                    exit_code=None,
                )
            )
    return states


def _parse_container_inspect(name: str, data: dict[str, Any]) -> ContainerState:
    state = data.get("State", {}) if isinstance(data, dict) else {}
    health_obj = state.get("Health") if isinstance(state, dict) else None
    health = health_obj.get("Status") if isinstance(health_obj, dict) else None
    return ContainerState(
        name=name,
        status=str(state.get("Status", "unknown")),
        oom_killed=bool(state.get("OOMKilled", False)),
        restart_count=_to_int(data.get("RestartCount")),
        health=health,
        exit_code=_to_int(state.get("ExitCode")) if state.get("Status") == "exited" else None,
    )


def container_to_signals(
    prev_restart: int | None,
    cur: ContainerState,
    *,
    restart_loop_threshold: int,
) -> list[Signal]:
    """Signals for a container state delta (Plan 2 §1.2, §3).

    Picks every applicable condition; OOM is the host's #1 risk so it's its own
    critical Signal. ``prev_restart`` is the last seen ``RestartCount`` from
    ``agent_probe_state`` (None on first observation) for the crash-loop delta.
    """
    signals: list[Signal] = []

    if cur.oom_killed:
        signals.append(
            Signal(
                source=AGENT_HEALTH_SOURCE,
                fingerprint=f"container-oom:{cur.name}",
                title=f"container {cur.name} OOMKilled",
                body=f"container: {cur.name}\nstatus: {cur.status}\noom_killed: true",
                suggested_severity="critical",
                meta={"kind": "container-oom", "container": cur.name},
            )
        )

    if prev_restart is not None:
        delta = cur.restart_count - prev_restart
        if delta >= restart_loop_threshold:
            signals.append(
                Signal(
                    source=AGENT_HEALTH_SOURCE,
                    fingerprint=f"container-crashloop:{cur.name}",
                    title=f"container {cur.name} crash-looping",
                    body=(
                        f"container: {cur.name}\nrestart_count: {cur.restart_count}\n"
                        f"delta_since_last_sweep: {delta} (threshold {restart_loop_threshold})"
                    ),
                    suggested_severity="critical",
                    meta={
                        "kind": "container-crashloop",
                        "container": cur.name,
                        "restart_delta": delta,
                    },
                )
            )

    if cur.status in {"exited", "dead", "missing"}:
        signals.append(
            Signal(
                source=AGENT_HEALTH_SOURCE,
                fingerprint=f"container-down:{cur.name}",
                title=f"container {cur.name} is {cur.status}",
                body=f"container: {cur.name}\nstatus: {cur.status}\nexit_code: {cur.exit_code}",
                suggested_severity="critical",
                meta={"kind": "container-down", "container": cur.name, "status": cur.status},
            )
        )
    elif cur.health == "unhealthy":
        signals.append(
            Signal(
                source=AGENT_HEALTH_SOURCE,
                fingerprint=f"container-unhealthy:{cur.name}",
                title=f"container {cur.name} unhealthy",
                body=f"container: {cur.name}\nstatus: {cur.status}\nhealth: unhealthy",
                suggested_severity="warning",
                meta={"kind": "container-unhealthy", "container": cur.name},
            )
        )

    return signals


# --- Batch/cron heartbeat freshness (Plan 2 §1.4) -------------------------- #


def heartbeat_to_signal(
    h: HeartbeatState, *, grace: float = 1.5, now: datetime | None = None
) -> Signal | None:
    """Signal for an overdue / failed batch run, or None if fresh & ok.

    Two failure shapes (Plan 2 §3): the last run's *status* is bad
    (failed/partial), or no successful run within ``expected_interval_s x grace``.
    ``expected_interval_s == 0`` means on-demand — never overdue.
    """
    now = now or datetime.now(UTC)

    # Never ran at all but a cadence is declared -> overdue.
    if h.last_finished is None:
        if h.expected_interval_s > 0:
            return Signal(
                source=AGENT_HEALTH_SOURCE,
                fingerprint=f"batch-overdue:{h.agent}",
                title=f"batch {h.agent} has never reported a run",
                body=(
                    f"agent: {h.agent}\n"
                    f"expected_interval_s: {h.expected_interval_s}\nlast_finished: never"
                ),
                suggested_severity="warning",
                meta={"kind": "batch-overdue", "agent": h.agent},
            )
        return None

    status = (h.last_status or "").lower()
    if status in {"failed", "partial"}:
        sev = "critical" if status == "failed" else "warning"
        return Signal(
            source=AGENT_HEALTH_SOURCE,
            fingerprint=f"batch-failed:{h.agent}",
            title=f"batch {h.agent} last run {status}",
            body=(
                f"agent: {h.agent}\nlast_status: {status}\n"
                f"last_finished: {h.last_finished.isoformat()}"
            ),
            suggested_severity=sev,
            meta={"kind": "batch-failed", "agent": h.agent, "status": status},
        )

    if h.expected_interval_s > 0:
        age_s = (now - h.last_finished).total_seconds()
        if age_s > h.expected_interval_s * grace:
            # x3 over the interval escalates to critical (Plan 2 §3).
            sev = "critical" if age_s > h.expected_interval_s * 3 else "warning"
            return Signal(
                source=AGENT_HEALTH_SOURCE,
                fingerprint=f"batch-overdue:{h.agent}",
                title=f"batch {h.agent} run overdue",
                body=(
                    f"agent: {h.agent}\nlast_finished: {h.last_finished.isoformat()}\n"
                    f"age_s: {int(age_s)} (interval {h.expected_interval_s}s x {grace})"
                ),
                suggested_severity=sev,
                meta={"kind": "batch-overdue", "agent": h.agent, "age_s": int(age_s)},
            )

    return None


async def collect_heartbeats(
    pool_for: Callable[[str], Awaitable[Any]],
    configs: dict[str, dict[str, Any]],
) -> list[HeartbeatState]:
    """Read each batch/cron agent's latest run heartbeat cross-DB (read-only).

    ``pool_for(db_name)`` is an async callable returning a connection pool bound
    to that agent's database; ``configs`` maps agent -> {db, table, interval_s}.
    Uses ``agentkit.heartbeat.last_beat`` (parameterized SQL) and never writes.
    A per-agent read failure is logged and skipped — one broken DB can't sink the
    whole sweep.
    """
    from agentkit.heartbeat import last_beat

    out: list[HeartbeatState] = []
    for agent, cfg in configs.items():
        db = cfg["db"]
        table = cfg.get("table", "run_heartbeats")
        job = cfg.get("job", agent)
        interval_s = int(cfg.get("interval_s", 0))
        try:
            pool = await pool_for(db)
            row = await last_beat(pool, job, table=table)
        except Exception as exc:
            log.warning("collectors.heartbeat_read_failed", agent=agent, db=db, error=str(exc))
            continue
        out.append(
            HeartbeatState(
                agent=agent,
                last_finished=row["ts"] if row else None,
                last_status=row["status"] if row else None,
                expected_interval_s=interval_s,
            )
        )
    return out


# --- RabbitMQ unconsumed queue depth (Plan 2 §1.5) ------------------------- #


async def collect_queue_depths(
    rabbit_mgmt_url: str,
    queues: list[str],
    *,
    client: httpx.AsyncClient,
    timeout: float = 5.0,  # noqa: ASYNC109 - configurable per-probe timeout is the API
) -> dict[str, int]:
    """Scrape ``messages_ready`` per watched queue from the RabbitMQ mgmt API.

    Reads ``GET /api/queues`` and filters to ``queues``. Returns name -> depth;
    queues not found are omitted (the bridge may not have declared them yet).
    """
    base = rabbit_mgmt_url.rstrip("/")
    depths: dict[str, int] = {}
    try:
        resp = await client.get(f"{base}/api/queues", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("collectors.rabbit_mgmt_failed", error=str(exc))
        return depths

    wanted = set(queues)
    for q in data if isinstance(data, list) else []:
        name = q.get("name")
        if name in wanted:
            depths[name] = _to_int(q.get("messages_ready"))
    return depths


def queue_to_signal(
    name: str,
    depth: int,
    prev_depth: int | None,
    *,
    threshold: int,
) -> Signal | None:
    """Signal when an alert queue backlog is over threshold AND still rising.

    A growing backlog on the alert bus is the canary for "alerts produced but
    nobody is delivering them" (Plan 2 §1.5) — i.e. the chat bridge consumer
    is dead. We require *rising* (depth >= prev) so a draining queue doesn't page.
    """
    if depth <= threshold:
        return None
    rising = prev_depth is None or depth >= prev_depth
    if not rising:
        return None
    return Signal(
        source=AGENT_HEALTH_SOURCE,
        fingerprint=f"queue-backlog:{name}",
        title=f"alert queue {name} backing up",
        body=(
            f"queue: {name}\nmessages_ready: {depth} (threshold {threshold})\n"
            f"prev_depth: {prev_depth}\nlikely cause: alert consumer (bridge) down"
        ),
        suggested_severity="critical",
        meta={"kind": "queue-backlog", "queue": name, "depth": depth, "prev_depth": prev_depth},
    )


# --- Host vitals (Plan 2 §1, RAM is the host constraint) ------------------- #


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse ``/proc/meminfo`` text into a {key: kB} dict (ints)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0])
    return out


def host_vitals_to_signals(
    meminfo: dict[str, int],
    *,
    mem_avail_warn_mb: int,
    swap_used_warn_mb: int,
) -> list[Signal]:
    """Signals for low available RAM / swap pressure (the host's real OOM risk).

    ``meminfo`` is the kB dict from :func:`parse_meminfo`. The upgrade trigger in
    Plan 0 §3 is sustained ``available < 1.5 GB`` or swap-in-use > 512 MiB.
    """
    signals: list[Signal] = []
    avail_kb = meminfo.get("MemAvailable")
    if avail_kb is not None:
        avail_mb = avail_kb // 1024
        if avail_mb < mem_avail_warn_mb:
            signals.append(
                Signal(
                    source=AGENT_HEALTH_SOURCE,
                    fingerprint="host-mem-low",
                    title=f"host RAM low: {avail_mb} MiB available",
                    body=f"MemAvailable: {avail_mb} MiB (warn < {mem_avail_warn_mb} MiB)",
                    suggested_severity="critical"
                    if avail_mb < mem_avail_warn_mb // 2
                    else "warning",
                    meta={"kind": "host-mem-low", "available_mb": avail_mb},
                )
            )

    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    swap_used_mb = (swap_total - swap_free) // 1024 if swap_total else 0
    if swap_used_mb > swap_used_warn_mb:
        signals.append(
            Signal(
                source=AGENT_HEALTH_SOURCE,
                fingerprint="host-swap-pressure",
                title=f"host swapping: {swap_used_mb} MiB in use",
                body=f"swap_used: {swap_used_mb} MiB (warn > {swap_used_warn_mb} MiB)",
                suggested_severity="warning",
                meta={"kind": "host-swap-pressure", "swap_used_mb": swap_used_mb},
            )
        )
    return signals


def read_host_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    """Best-effort read of host meminfo; empty dict if unavailable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_meminfo(fh.read())
    except OSError as exc:
        log.warning("collectors.meminfo_read_failed", path=path, error=str(exc))
        return {}
