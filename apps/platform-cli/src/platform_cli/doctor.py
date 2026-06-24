"""``platform doctor`` — probe each registered agent's ``/healthz`` + ``/readyz``.

Mirrors the dashboard aggregator's probing: it skips ``kind == "batch"`` /
``port == 0`` agents (no port, freshness is heartbeat-only) and **reports** an
unreachable agent rather than raising — a missing agent is a degraded fleet, not
a CLI crash (US-013 graceful degradation).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from dashboard.registry import AgentSpec

from . import config

PROBE_TIMEOUT_S = 3.0


@dataclass
class ProbeResult:
    slug: str
    skipped: bool = False
    reason: str = ""  # why skipped, e.g. "batch, no port"
    healthz_ok: bool | None = None
    readyz_ok: bool | None = None
    error: str | None = None


def _should_skip(spec: AgentSpec) -> bool:
    """Batch agents / port-less agents are heartbeat-only — not HTTP-probed."""
    return bool(spec.kind == "batch" or spec.port == 0)


async def _probe_one(client: httpx.AsyncClient, spec: AgentSpec) -> ProbeResult:
    if _should_skip(spec):
        return ProbeResult(slug=spec.slug, skipped=True, reason="batch, no port")
    result = ProbeResult(slug=spec.slug)
    try:
        health = await client.get(f"{spec.base_url}/healthz")
        result.healthz_ok = health.status_code < 400
    except httpx.HTTPError as exc:
        result.healthz_ok = False
        result.error = type(exc).__name__
        return result
    try:
        ready = await client.get(f"{spec.base_url}/readyz")
        result.readyz_ok = ready.status_code < 400
    except httpx.HTTPError as exc:
        result.readyz_ok = False
        result.error = type(exc).__name__
    return result


async def probe(
    specs: Sequence[AgentSpec], *, timeout_s: float = PROBE_TIMEOUT_S
) -> list[ProbeResult]:
    """Probe every spec concurrently; never raises on an unreachable agent."""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        return [await coro for coro in [_probe_one(client, s) for s in specs]]


def _registry(env_path: Path) -> list[AgentSpec]:
    return config.registry_for(config.load_agents(env_path))


def doctor(
    *,
    slug: str | None = None,
    as_json: bool = False,
    env_path: Path = config.DASHBOARD_ENV_PATH,
) -> int:
    """Probe the registry (optionally one ``--slug``) and print the report."""
    specs = _registry(env_path)
    if slug is not None:
        specs = [s for s in specs if s.slug == slug]
        if not specs:
            print(f"error: no agent with slug {slug!r}")
            return 1

    results = asyncio.run(probe(specs))

    if as_json:
        print(
            json.dumps(
                [
                    {
                        "slug": r.slug,
                        "skipped": r.skipped,
                        "reason": r.reason,
                        "healthz_ok": r.healthz_ok,
                        "readyz_ok": r.readyz_ok,
                        "error": r.error,
                    }
                    for r in results
                ],
                separators=(",", ":"),
            )
        )
        return 0

    if not results:
        print("(no agents registered)")
        return 0

    for r in results:
        if r.skipped:
            print(f"{r.slug}  ({r.reason}) — skipped, heartbeat-only")
            continue
        health = "✓" if r.healthz_ok else "✗"
        ready = "✓" if r.readyz_ok else "✗"
        tail = f"   ({r.error})" if r.error else ""
        print(f"{r.slug}  healthz {health}   readyz {ready}{tail}")
    return 0
