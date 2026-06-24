"""Pure-ish view builders: turn aggregator results into template context dicts.

Separated from the FastAPI routes so the rendering logic is unit-testable with a
mocked :class:`~dashboard.aggregator.Aggregator` and no HTTP server. Each
function fans out to the aggregator and returns the ``dict`` a Jinja partial
consumes — including the graceful ``unavailable`` marker when an agent is down.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from agentkit import audit
from agentkit.egress import _split_hostport

from . import store
from .aggregator import Aggregator, HealthDTO, Unavailable
from .registry import AgentSpec, by_slug, with_feature


def summarize_agents(agents: list[HealthDTO]) -> dict[str, int]:
    healthy = sum(1 for a in agents if a.status == "green")
    degraded = sum(1 for a in agents if a.status == "amber")
    down = sum(1 for a in agents if a.status == "red")
    return {"healthy": healthy, "degraded": degraded, "down": down}


# Number of categorical series colors (--chart-1..--chart-6 in tokens.css). Model
# color index cycles through this set when models exceed it.
_CHART_COLORS = 6


def build_cost_chart(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pivot per-(day, model) cost rows into a stacked-bar structure: one bar per
    day, model segments sized as a percent of the tallest day's total, plus a
    legend ordered by total spend. Pure/deterministic for easy testing."""
    days_map: dict[Any, dict[str, float]] = {}
    model_total: dict[str, float] = {}
    for r in rows:
        day, model, usd = r["day"], r["model"], float(r["usd"] or 0.0)
        days_map.setdefault(day, {})[model] = days_map.get(day, {}).get(model, 0.0) + usd
        model_total[model] = model_total.get(model, 0.0) + usd
    models_sorted = sorted(model_total, key=lambda m: (-model_total[m], m))
    color_of = {m: i % _CHART_COLORS for i, m in enumerate(models_sorted)}
    max_total = max((sum(segs.values()) for segs in days_map.values()), default=0.0)
    days: list[dict[str, Any]] = []
    for day in sorted(days_map):
        segs = days_map[day]
        total = sum(segs.values())
        segments = [
            {
                "model": m,
                "usd": round(segs[m], 6),
                "color_idx": color_of[m],
                "pct": round(segs[m] / total * 100, 2) if total else 0.0,
            }
            for m in sorted(segs, key=lambda m: (-segs[m], m))
        ]
        days.append(
            {
                "label": day.strftime("%m-%d") if hasattr(day, "strftime") else str(day),
                "total": round(total, 6),
                "height_pct": round(total / max_total * 100, 1) if max_total else 0.0,
                "segments": segments,
            }
        )
    legend = [
        {"model": m, "color_idx": color_of[m], "total": round(model_total[m], 6)}
        for m in models_sorted
    ]
    return {"days": days, "legend": legend, "total": round(sum(model_total.values()), 6)}


async def overview_context(
    agg: Aggregator, registry: list[AgentSpec], *, pool: Any | None = None
) -> dict[str, Any]:
    agents = await agg.agents_health(registry)
    summary: dict[str, float] = dict(summarize_agents(agents))

    cost_chart: dict[str, Any] = {"days": [], "legend": [], "total": 0.0}
    cost_chart_agent: dict[str, Any] = {"days": [], "legend": [], "total": 0.0}
    if pool is not None:
        with contextlib.suppress(Exception):
            cost_chart = build_cost_chart(await store.cost_model_daily(pool, days=14))
        with contextlib.suppress(Exception):
            cost_chart_agent = build_cost_chart(await store.cost_agent_daily(pool, days=14))

    open_incidents = 0
    high_findings = 0
    mon = with_feature(registry, "incidents")
    if mon is not None:
        inc = await agg.incidents(mon)
        if not isinstance(inc, Unavailable):
            open_incidents = sum(1 for i in inc.get("incidents", []) if i.get("status") == "open")
    sec = with_feature(registry, "findings")
    if sec is not None:
        fnd = await agg.findings(sec, severity="high")
        if not isinstance(fnd, Unavailable):
            high_findings = len(fnd.get("findings", []))

    llm_spend = sum(a.llm_cost_usd_today or 0.0 for a in agents)
    summary.update(
        open_incidents=open_incidents,
        high_findings=high_findings,
        llm_spend_today=round(llm_spend, 4),
    )
    return {
        "agents": agents,
        "summary": summary,
        "cost_chart": cost_chart,
        "cost_chart_agent": cost_chart_agent,
    }


async def incidents_context(
    agg: Aggregator,
    registry: list[AgentSpec],
    *,
    limit: int = 50,
    pool: Any | None = None,
    tenant: str | None = None,
) -> dict[str, Any]:
    """Live incidents from the monitoring agent, merged with the dashboard's own
    triage overlay (ack/resolve/snooze — the monitoring agent has no such
    endpoint, so the operator's decision lives here). ``triage`` is a map keyed
    by the incident's stable ``dedup_key``; the template reads it to render a
    status badge and demote resolved rows."""
    mon = with_feature(registry, "incidents")
    if mon is None:
        return {
            "unavailable": "no incidents-capable agent in registry",
            "incidents": [],
            "triage": {},
        }
    res = await agg.incidents(mon, limit=limit)
    if isinstance(res, Unavailable):
        return {"unavailable": res.reason, "incidents": [], "triage": {}}
    triage = (
        await store.incident_triage_map(pool, tenant_id=tenant)
        if pool is not None and tenant is not None
        else {}
    )
    return {"unavailable": None, "incidents": res.get("incidents", []), "triage": triage}


async def findings_context(
    agg: Aggregator, registry: list[AgentSpec], *, severity: str | None = None
) -> dict[str, Any]:
    sec = with_feature(registry, "findings")
    if sec is None:
        return {"unavailable": "no findings-capable agent in registry", "findings": []}
    res = await agg.findings(sec, severity=severity)
    if isinstance(res, Unavailable):
        return {"unavailable": res.reason, "findings": []}
    return {"unavailable": None, "findings": res.get("findings", [])}


async def qa_context(
    agg: Aggregator, registry: list[AgentSpec], *, severity: str | None = None
) -> dict[str, Any]:
    """QA report for the coverage-capable (QA tester) agent — resolved by the
    ``coverage`` capability flag, NOT a literal slug, so it stays distinct from
    the security ``findings`` feed while remaining agent-agnostic. Surfaces its
    findings + coverage rollup."""
    spec = with_feature(registry, "coverage")
    if spec is None:
        return {
            "unavailable": "no coverage-capable agent in registry",
            "findings": [],
            "coverage": None,
        }
    res = await agg.findings(spec, severity=severity)
    if isinstance(res, Unavailable):
        return {"unavailable": res.reason, "findings": [], "coverage": None, "counts": {}}
    cov = await agg.coverage(spec)
    coverage = None if isinstance(cov, Unavailable) else cov
    counts = res.get("counts", {}) if isinstance(res, dict) else {}
    return {
        "unavailable": None,
        "findings": res.get("findings", []),
        "coverage": coverage,
        "counts": counts,
        "auto_closed": counts.get("dismissed", 0),
    }


async def pipeline_context(agg: Aggregator, registry: list[AgentSpec]) -> dict[str, Any]:
    wx = with_feature(registry, "runs")
    if wx is None:
        return {"unavailable": "no runs-capable agent in registry", "runs": []}
    res = await agg.webext_runs(wx)
    if isinstance(res, Unavailable):
        return {"unavailable": res.reason, "runs": []}
    return {"unavailable": None, "runs": res.get("runs", [])}


async def pm_context(agg: Aggregator, registry: list[AgentSpec]) -> dict[str, Any]:
    pm = with_feature(registry, "pm")
    if pm is None:
        return {"unavailable": "no pm-capable agent in registry", "digests": [], "action_items": []}
    digests = await agg.pm_digests(pm)
    if isinstance(digests, Unavailable):
        return {"unavailable": digests.reason, "digests": [], "action_items": []}
    items = await agg.pm_action_items(pm)
    items_list = [] if isinstance(items, Unavailable) else items.get("action_items", [])
    return {
        "unavailable": None,
        "digests": digests.get("digests", []),
        "action_items": items_list,
    }


async def cost_context(
    agg: Aggregator, registry: list[AgentSpec], *, pool: Any | None = None
) -> dict[str, Any]:
    rollup = await agg.cost_rollup(registry)
    windows: list[dict[str, Any]] = []
    if pool is not None:
        with contextlib.suppress(Exception):
            windows = await store.cost_windows(pool)
    totals = {
        "week": round(sum(w["week"] for w in windows), 6),
        "month": round(sum(w["month"] for w in windows), 6),
        "all_time": round(sum(w["all_time"] for w in windows), 6),
    }
    return {"rollup": rollup, "windows": windows, "totals": totals}


async def agent_detail_context(
    agg: Aggregator,
    registry: list[AgentSpec],
    *,
    slug: str,
    pool: Any | None = None,
) -> dict[str, Any]:
    """Per-agent detail (AG-1): the live health DTO + declared capabilities +
    a recent up/down heartbeat strip from the dashboard's own time-series. The
    ``unavailable`` state covers an unknown slug (not in the registry)."""
    spec = by_slug(registry, slug)
    if spec is None:
        return {
            "unavailable": f"no agent '{slug}' in the agents registry",
            "slug": slug,
            "health": None,
        }
    health = await agg.health(spec)
    heartbeats = await store.recent_heartbeats(pool, slug, limit=48) if pool is not None else []
    return {
        "unavailable": None,
        "slug": slug,
        "health": health,
        "capabilities": _agent_capabilities(spec),
        "heartbeats": heartbeats,
    }


# Display-only capability labels per role (Pattern 2 — capabilities grid).
# These describe what a role CAN do in the console; they are NOT an authz source
# (enforcement is the JWT role check in auth.verify_admin_token).
ROLE_CAPABILITIES: dict[str, list[str]] = {
    "admin": [
        "view all agents data",
        "run sweeps & scans",
        "mint/revoke tokens",
        "export audit log",
    ],
    "manager": ["view all agents data", "run sweeps & scans"],
    "viewer": ["view agents data", "read activity log"],
    "service": ["expose declared capabilities to the dashboard"],
}

# Map an agent's declared feature flags to human capability labels for the grid.
_FEATURE_LABELS: dict[str, str] = {
    "has_incidents": "incidents",
    "has_findings": "findings",
    "has_runs": "pipeline runs",
    "has_pm": "PM digests",
}


def _agent_capabilities(spec: AgentSpec) -> list[str]:
    return [label for attr, label in _FEATURE_LABELS.items() if getattr(spec, attr, False)]


def workspace_context(registry: list[AgentSpec], *, principal: Any) -> dict[str, Any]:
    """Members + capabilities for the current workspace (Pattern 2).

    Read-only: the human operator (from the Principal) plus every registered
    agent as a service account, each with a role badge and capability list.
    ``unavailable`` when the agents registry is empty (nothing to show).
    """
    members: list[dict[str, Any]] = [
        {
            "name": principal.sub,
            "kind": "human",
            "role": principal.role,
            "status": "active",
            "capabilities": ROLE_CAPABILITIES.get(principal.role, []),
        }
    ]
    for spec in registry:
        members.append(
            {
                "name": spec.slug,
                "kind": "self" if spec.kind == "self" else "service",
                "role": "service",
                "status": spec.kind,
                "capabilities": _agent_capabilities(spec) or ["—"],
            }
        )
    capabilities = [
        {"role": role, "can": caps} for role, caps in ROLE_CAPABILITIES.items() if role != "service"
    ]
    unavailable = None if registry else "no agents registered in the agents"
    return {
        "tenant": principal.tenant,
        "members": members,
        "capabilities": capabilities,
        "unavailable": unavailable,
    }


def privacy_context(settings: Any) -> dict[str, Any]:
    """Local-first trust panel tied to Private Mode.

    Reports whether ``PRIVATE_MODE`` is on and lists the egress allow-list
    (host:port only — never the raw DSN, which embeds passwords). The label↔host
    pairing uses ``_split_hostport`` per source and SKIPS unparseable URLs, so an
    empty/garbled DSN can't mislabel another host (it just drops out).
    """
    sources = [
        ("LLM gateway", settings.llm_base_url),
        ("Postgres", settings.database_url),
        ("Redis", settings.redis_url),
        ("RabbitMQ", settings.rabbitmq_url),
    ]
    allowed: list[dict[str, Any]] = []
    for name, url in sources:
        host, port = _split_hostport(url or "")
        if host is not None and port is not None:
            allowed.append({"name": name, "host": host, "port": port})
    return {
        "private_mode": bool(settings.private_mode),
        "otel_enabled": bool(settings.effective_otel_enabled),
        "allowed": allowed,
    }


async def activity_context(
    pool: Any,
    *,
    tenant: str,
    action: str | None = None,
    actor: str | None = None,
    q: str | None = None,
    before: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Activity-stream context (Pattern 4): the tenant-scoped audit feed with
    filters (AU-1). ``action``/``actor`` filter in the DB; ``q`` is a free-text
    substring match over actor/action/resource applied to the page; ``before``
    is the keyset cursor for "load older". The selected filters + the distinct
    action list (for the dropdown) + the next-page cursor are returned so the
    partial re-renders with its controls intact.

    Graceful state: DB pool ``None`` (unreachable at boot) → ``unavailable``.
    """
    if pool is None:
        return {
            "unavailable": "database is unavailable",
            "events": [],
            "actions": [],
            "filter": {"action": action, "actor": actor, "q": q},
            "next_before": None,
        }
    raw = await audit.query(
        pool, tenant_id=tenant, action=action, actor=actor, before=before, limit=limit
    )
    # Keyset cursor is computed from the RAW DB page (before the in-Python q
    # filter shrinks it): "load older" must follow the true page boundary, else
    # q-filtered views can never page past the first DB page. The cursor is the
    # oldest DB row's ts (rows are newest-first).
    has_more = len(raw) >= limit
    next_before = raw[-1]["ts"].isoformat() if has_more and raw else None
    events = raw
    if q:
        needle = q.strip().lower()

        def _hay(e: dict[str, Any]) -> str:
            return f"{e.get('actor', '')} {e.get('action', '')} {e.get('resource') or ''}".lower()

        events = [e for e in raw if needle in _hay(e)]
    actions = await audit.distinct_actions(pool, tenant_id=tenant)
    return {
        "unavailable": None,
        "events": events,
        "actions": actions,
        "filter": {"action": action, "actor": actor, "q": q},
        "next_before": next_before,
    }
