"""Aggregation layer — the dashboard as an HTTP client of the agents (Plan 4 §3).

For every panel the dashboard calls a sibling agent's HTTP API with its own
minted admin token, applies a short timeout, and returns either a typed payload
or a typed :class:`Unavailable` marker. If an agent is down the dashboard *sees*
it down (a 503/timeout) — which is exactly the meta-monitoring signal we want
(Plan 4 §3): the overview renders that as a red/grey tile rather than blanking.

Responses are cached in Redis (DB index 4, the dashboard's own) with a short TTL
so a 10s auto-refresh + a human reload don't fan out N upstream calls each time.
The cache is best-effort: a Redis miss/blip just means we hit the agent directly.

It never reads another agent's Postgres tables (only HTTP), honoring per-agent DB
isolation. The dashboard's own DB (heartbeats / cost rollup) is written elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
from agentkit.observability import get_logger

from .auth import UpstreamToken
from .registry import AgentSpec
from .settings import Settings

log = get_logger("dashboard.aggregator")


@dataclass(frozen=True)
class Unavailable:
    """Typed 'agent unreachable / errored' marker rendered as a degraded tile."""

    slug: str
    reason: str  # e.g. "timeout", "connect", "http 503", "http 404"

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class AlreadyRunning:
    """Typed marker for the web-ext control server's 409 'a run is in progress'.

    Distinct from :class:`Unavailable` so the UI can show a benign "already
    running" notice instead of an error tile (the control server serializes runs
    on the RAM-constrained host, so a 409 is expected, not a failure).
    """

    slug: str

    @property
    def ok(self) -> bool:
        return False


@dataclass
class HealthDTO:
    """Composed health for one agent's overview tile (Plan 4 §2.1, §8)."""

    slug: str
    title: str
    port: int
    kind: str
    # Declared capabilities (mirrored from the registry spec) so the overview
    # tile can render links/actions per-capability, not per hardcoded slug.
    has_incidents: bool = False
    has_findings: bool = False
    has_runs: bool = False
    has_pm: bool = False
    live: bool = False  # /healthz answered 200
    ready: bool | None = None  # /readyz: True/False; None if not probed/unknown
    checks: dict[str, bool] = field(default_factory=dict)  # readyz sub-checks
    uptime_s: float | None = None
    error_rate_5m: float | None = None
    requests_5m: int | None = None
    llm_cost_usd_today: float | None = None
    cost_by_model_today: dict[str, float] | None = None  # {model: usd} split
    last_run: dict[str, Any] | None = None  # for batch agents
    error: str | None = None  # populated when the agent is unreachable

    @property
    def status(self) -> str:
        """green | amber | red | grey per the §8 legend."""
        if self.kind == "batch":
            return "grey"
        if not self.live:
            return "red"
        if self.ready is False or (self.error_rate_5m or 0.0) > 0.1:
            return "amber"
        return "green"


class Aggregator:
    """Owns the shared httpx client, the upstream token, and the Redis cache."""

    def __init__(
        self,
        settings: Settings,
        token: UpstreamToken,
        *,
        client: httpx.AsyncClient | None = None,
        redis: Any | None = None,
    ) -> None:
        self._settings = settings
        self._token = token
        self._redis = redis
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.upstream_timeout_s)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- low-level upstream GET --------------------------------------------
    async def _get(self, spec: AgentSpec, path: str) -> dict[str, Any] | Unavailable:
        url = f"{spec.base_url}{path}"
        try:
            resp = await self._client.get(url, headers=self._token.auth_header())
        except httpx.TimeoutException:
            return Unavailable(spec.slug, "timeout")
        except httpx.HTTPError as exc:
            return Unavailable(spec.slug, f"connect: {type(exc).__name__}")
        if resp.status_code >= 400:
            return Unavailable(spec.slug, f"http {resp.status_code}")
        try:
            return cast("dict[str, Any]", resp.json())
        except json.JSONDecodeError:
            return Unavailable(spec.slug, "bad json")

    async def _post(
        self, spec: AgentSpec, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any] | Unavailable:
        url = f"{spec.base_url}{path}"
        try:
            resp = await self._client.post(url, headers=self._token.auth_header(), json=body)
        except httpx.TimeoutException:
            return Unavailable(spec.slug, "timeout")
        except httpx.HTTPError as exc:
            return Unavailable(spec.slug, f"connect: {type(exc).__name__}")
        if resp.status_code >= 400:
            return Unavailable(spec.slug, f"http {resp.status_code}")
        try:
            return cast("dict[str, Any]", resp.json())
        except json.JSONDecodeError:
            return {"ok": True}

    # --- Redis cache (best-effort) -----------------------------------------
    async def _cache_get(self, key: str) -> Any | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(f"dash:cache:{key}")
        except Exception as exc:
            log.warning("cache.get_failed", key=key, error=str(exc))
            return None
        return json.loads(raw) if raw else None

    async def _cache_set(self, key: str, value: Any) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                f"dash:cache:{key}", json.dumps(value), ex=self._settings.cache_ttl_s
            )
        except Exception as exc:
            log.warning("cache.set_failed", key=key, error=str(exc))

    def _apply_metrics(self, dto: HealthDTO, metrics: dict[str, Any]) -> None:
        """Fold a /metrics.json payload into the tile (shared by all kinds)."""
        dto.uptime_s = metrics.get("uptime_s")
        dto.error_rate_5m = metrics.get("error_rate_5m")
        dto.requests_5m = metrics.get("requests_5m")
        dto.llm_cost_usd_today = metrics.get("llm_cost_usd_today")
        dto.cost_by_model_today = metrics.get("cost_by_model_today")
        dto.last_run = metrics.get("last_run")

    # --- health (overview) --------------------------------------------------
    async def health(self, spec: AgentSpec) -> HealthDTO:
        """Compose one agent's tile from /healthz + /readyz + /metrics.json.

        The three probes are fired CONCURRENTLY (one round-trip's latency, not
        three) — they are independent reads and the dashboard fans out over O(N)
        agents, so serializing them needlessly tripled overview latency.
        """
        dto = HealthDTO(
            slug=spec.slug,
            title=spec.title,
            port=spec.port,
            kind=spec.kind,
            has_incidents=spec.has_incidents,
            has_findings=spec.has_findings,
            has_runs=spec.has_runs,
            has_pm=spec.has_pm,
        )

        # A batch agent has no port: "down" is normal — its freshness comes from a
        # last_run heartbeat exposed via /metrics.json only (no /healthz|/readyz).
        if spec.kind == "batch":
            metrics = await self._get(spec, "/metrics.json")
            if not isinstance(metrics, Unavailable):
                self._apply_metrics(dto, metrics)
            return dto

        live, readyz, metrics = await asyncio.gather(
            self._get(spec, "/healthz"),
            self._get(spec, "/readyz"),
            self._get(spec, "/metrics.json"),
        )

        dto.live = not isinstance(live, Unavailable)
        if isinstance(live, Unavailable):
            dto.error = live.reason
            return dto

        if isinstance(readyz, Unavailable):
            # /readyz returns 503 (not 200) when not ready -> _get marks Unavailable;
            # treat an http-503 specifically as "not ready", not "unreachable".
            dto.ready = False if readyz.reason == "http 503" else None
        else:
            dto.ready = bool(readyz.get("ready"))
            dto.checks = readyz.get("checks", {}) or {}

        if not isinstance(metrics, Unavailable):
            self._apply_metrics(dto, metrics)
        return dto

    async def agents_health(
        self, registry: list[AgentSpec], *, refresh: bool = False
    ) -> list[HealthDTO]:
        """Probe every agent concurrently for the overview board, serving the
        short-TTL Redis cache so a 10s auto-refresh + a human reload don't each
        fan out O(N) upstream calls.

        ``refresh=True`` skips the cache READ (forcing a fresh fan-out) but still
        WRITES the result back — so a periodic snapshot can refresh the shared
        cache coherently rather than racing a separate cold fan-out against it.
        """
        if not refresh:
            cached = await self._cache_get("agents_health")
            if cached is not None:
                return [_health_from_dict(d) for d in cached]
        dtos = await asyncio.gather(*(self.health(s) for s in registry))
        await self._cache_set("agents_health", [_health_to_dict(d) for d in dtos])
        return list(dtos)

    # --- panel reads (incidents/findings/runs/pm) ---------------------------
    async def incidents(self, spec: AgentSpec, *, limit: int = 50) -> dict[str, Any] | Unavailable:
        return await self._get(spec, f"/incidents?limit={int(limit)}")

    async def findings(
        self, spec: AgentSpec, *, severity: str | None = None
    ) -> dict[str, Any] | Unavailable:
        path = "/findings"
        if severity:
            path += f"?severity={severity}"
        return await self._get(spec, path)

    async def coverage(self, spec: AgentSpec) -> dict[str, Any] | Unavailable:
        return await self._get(spec, "/coverage")

    async def resolve_finding(
        self,
        spec: AgentSpec,
        *,
        dedup_key: str,
        status: str,
        by: str,
        note: str | None = None,
    ) -> dict[str, Any] | Unavailable:
        """Proxy an operator's finding-triage decision to the agent's existing
        ``POST /findings/resolve`` feedback channel (the agent owns finding
        state; the dashboard never writes its DB). ``status`` is one of the
        agent's FEEDBACK_STATUSES (fixed/dismissed/wontfix/confirmed/needs_human).
        """
        return await self._post(
            spec,
            "/findings/resolve",
            body={"dedup_key": dedup_key, "status": status, "by": by, "note": note},
        )

    async def report_latest(self, spec: AgentSpec) -> str | Unavailable:
        url = f"{spec.base_url}/report/latest"
        try:
            resp = await self._client.get(url, headers=self._token.auth_header())
        except httpx.HTTPError as exc:
            return Unavailable(spec.slug, f"connect: {type(exc).__name__}")
        if resp.status_code >= 400:
            return Unavailable(spec.slug, f"http {resp.status_code}")
        return resp.text

    async def runs(self, spec: AgentSpec, *, limit: int = 20) -> dict[str, Any] | Unavailable:
        return await self._get(spec, f"/runs?limit={int(limit)}")

    async def pm_digests(self, spec: AgentSpec) -> dict[str, Any] | Unavailable:
        return await self._get(spec, "/digests")

    async def pm_action_items(self, spec: AgentSpec) -> dict[str, Any] | Unavailable:
        return await self._get(spec, "/action-items")

    # --- actions (sweep / scan) --------------------------------------------
    async def run_sweep(self, spec: AgentSpec) -> dict[str, Any] | Unavailable:
        return await self._post(spec, "/sweep")

    async def run_scan(self, spec: AgentSpec) -> dict[str, Any] | Unavailable:
        return await self._post(spec, "/scan")

    # --- web-ext pipeline control (Plan 4 §3.3) ----------------------------
    async def webext_run(
        self, spec: AgentSpec, *, limit: int = 1
    ) -> dict[str, Any] | AlreadyRunning | Unavailable:
        """Trigger a bounded pipeline run on the web-ext control server.

        Defaults to ``limit=1`` because a dashboard-triggered run shares the
        RAM-constrained host (ollama embed bursts); a human can widen scope from
        the control server directly. Returns the ``{run_id,status}`` payload, an
        :class:`AlreadyRunning` marker on a 409 (a run is already in progress —
        the control server serializes runs), or :class:`Unavailable` otherwise.
        """
        url = f"{spec.base_url}/run"
        try:
            resp = await self._client.post(
                url, headers=self._token.auth_header(), json={"limit": int(limit)}
            )
        except httpx.TimeoutException:
            return Unavailable(spec.slug, "timeout")
        except httpx.HTTPError as exc:
            return Unavailable(spec.slug, f"connect: {type(exc).__name__}")
        if resp.status_code == 409:
            return AlreadyRunning(spec.slug)
        if resp.status_code >= 400:
            return Unavailable(spec.slug, f"http {resp.status_code}")
        try:
            return cast("dict[str, Any]", resp.json())
        except json.JSONDecodeError:
            return Unavailable(spec.slug, "bad json")

    async def webext_runs(self, spec: AgentSpec) -> dict[str, Any] | Unavailable:
        """List recent control-server runs. The control server returns a JSON
        array ``[{run_id,status,domains,count,cost_usd,started_at,ended_at}]``;
        normalize to ``{"runs": [...]}`` so the template iterates uniformly."""
        res = await self._get(spec, "/runs")
        if isinstance(res, Unavailable):
            return res
        if isinstance(res, list):
            return {"runs": res}
        return {"runs": res.get("runs", [])}

    # --- cost rollup --------------------------------------------------------
    async def cost_rollup(self, registry: list[AgentSpec]) -> dict[str, Any]:
        """Aggregate today's LLM spend per agent from each /metrics.json (MVP,
        Plan 4 §3.4 metrics-snapshot path). Unreachable agents contribute 0 and
        are listed under ``unavailable``."""
        cached = await self._cache_get("cost_rollup")
        if cached is not None:
            return cast("dict[str, Any]", cached)
        results = await asyncio.gather(*(self._get(s, "/metrics.json") for s in registry))
        per_agent: dict[str, float] = {}
        unavailable: list[str] = []
        total = 0.0
        for spec, res in zip(registry, results, strict=False):
            if spec.kind == "self":
                continue
            if isinstance(res, Unavailable):
                unavailable.append(spec.slug)
                continue
            usd = float(res.get("llm_cost_usd_today") or 0.0)
            per_agent[spec.slug] = round(usd, 6)
            total += usd
        rollup = {
            "per_agent": per_agent,
            "total_usd_today": round(total, 6),
            "unavailable": unavailable,
            "computed_at": time.time(),
        }
        await self._cache_set("cost_rollup", rollup)
        return rollup


# --- helpers ----------------------------------------------------------------
def _health_to_dict(d: HealthDTO) -> dict[str, Any]:
    return {
        "slug": d.slug,
        "title": d.title,
        "port": d.port,
        "kind": d.kind,
        "has_incidents": d.has_incidents,
        "has_findings": d.has_findings,
        "has_runs": d.has_runs,
        "has_pm": d.has_pm,
        "live": d.live,
        "ready": d.ready,
        "checks": d.checks,
        "uptime_s": d.uptime_s,
        "error_rate_5m": d.error_rate_5m,
        "requests_5m": d.requests_5m,
        "llm_cost_usd_today": d.llm_cost_usd_today,
        "cost_by_model_today": d.cost_by_model_today,
        "last_run": d.last_run,
        "error": d.error,
    }


def _health_from_dict(d: dict[str, Any]) -> HealthDTO:
    return HealthDTO(**d)
