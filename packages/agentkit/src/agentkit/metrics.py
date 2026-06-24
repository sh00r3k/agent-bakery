"""In-process observability metrics for the /metrics.json contract.

Every agent's FastAPI app (built via ``agentkit.server.create_app``) exposes a
uniform ``GET /metrics.json`` that the dashboard and the meta-monitoring agent
consume without per-agent special-casing. The shape is fixed by the agents:

    {
      "agent": str,
      "uptime_s": float,
      "error_rate_5m": float,        # errors / requests over a rolling 5m window
      "last_run": {"ts": str, "status": str} | null,  # scheduled/batch agents
      "llm_cost_usd_today": float,
      "custom": { ... }              # agent-registered extra metrics
    }

Design constraints (Plan 0 §3 — small host, ADR-0001 our-own-infra):
- Dependency-free: a hand-rolled rolling-window counter, NO prometheus client.
- In-process: counters live on ``MetricsRegistry``; nothing is persisted here
  (cross-restart history is the dashboard's ``heartbeats`` table, fed by
  snapshotting this endpoint; durable run freshness is ``agentkit.heartbeat``).
- Cheap: the request/error counter is wired as a single FastAPI middleware.

Agents register extra metrics or a ``last_run`` provider via the registry that
``create_app`` stashes on ``app.state.metrics``.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Any

MetricValue = float | int | str | bool | None
# A custom-metric provider is any zero-arg callable (sync or async) returning a
# JSON-safe value. Async providers are awaited by the endpoint.
MetricProvider = Callable[[], MetricValue] | Callable[[], Awaitable[MetricValue]]
# A last_run provider returns {"ts": iso, "status": str} | None (sync or async).
LastRunValue = dict[str, Any] | None
LastRunProvider = Callable[[], LastRunValue] | Callable[[], Awaitable[LastRunValue]]


class RollingCounter:
    """Count timestamped events over a fixed trailing window (seconds).

    Records monotonic timestamps in a deque and evicts anything older than the
    window on each read/write. Thread-safe (FastAPI may run sync middleware in a
    worker thread). Memory is bounded by event rate * window, which for agent
    request volumes is trivial; if that ever matters, switch to fixed buckets.
    """

    def __init__(self, window_s: float = 300.0) -> None:
        self.window_s = float(window_s)
        self._events: deque[float] = deque()
        self._lock = Lock()

    def _now(self) -> float:
        # monotonic so a wall-clock step (NTP) can't skew the window
        return time.monotonic()

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        events = self._events
        while events and events[0] < cutoff:
            events.popleft()

    def incr(self, n: int = 1) -> None:
        now = self._now()
        with self._lock:
            self._evict(now)
            for _ in range(max(0, n)):
                self._events.append(now)

    def count(self) -> int:
        now = self._now()
        with self._lock:
            self._evict(now)
            return len(self._events)


class MetricsRegistry:
    """Per-process metrics state stashed on ``app.state.metrics``.

    Tracks a rolling 5-minute request/error window, exposes the process uptime,
    and lets an agent plug in:
      - ``register("name", provider)`` — an extra value under ``custom``;
      - ``set_last_run_provider(provider)`` — the scheduled/batch ``last_run``;
      - ``set_llm_usage(usage_obj)`` — an object with a float ``cost_usd`` that
        accumulates today's spend (typically ``app.state.llm.usage``).

    The request/error counters are driven by the middleware in ``create_app``;
    agents that do their work off the HTTP path (cron, consumers) can also call
    ``record_request`` / ``record_error`` directly.
    """

    def __init__(self, agent_name: str, *, window_s: float = 300.0) -> None:
        self.agent_name = agent_name
        self._started_monotonic = time.monotonic()
        self.started_at = time.time()
        self._requests = RollingCounter(window_s)
        self._errors = RollingCounter(window_s)
        self._custom: dict[str, MetricProvider] = {}
        self._last_run_provider: LastRunProvider | None = None
        self._llm_usage: Any | None = None

    # --- counters -----------------------------------------------------------
    def record_request(self, n: int = 1) -> None:
        self._requests.incr(n)

    def record_error(self, n: int = 1) -> None:
        self._errors.incr(n)

    def requests_5m(self) -> int:
        return self._requests.count()

    def errors_5m(self) -> int:
        return self._errors.count()

    def error_rate_5m(self) -> float:
        """errors / requests over the window. 0.0 when there were no requests."""
        reqs = self._requests.count()
        if reqs <= 0:
            return 0.0
        return self._errors.count() / reqs

    def uptime_s(self) -> float:
        return time.monotonic() - self._started_monotonic

    # --- pluggable surfaces -------------------------------------------------
    def register(self, name: str, provider: MetricProvider) -> None:
        """Register an extra metric surfaced under ``custom[name]``."""
        self._custom[name] = provider

    def set_last_run_provider(self, provider: LastRunProvider) -> None:
        self._last_run_provider = provider

    def set_llm_usage(self, usage: Any) -> None:
        """Track an accumulating usage object (e.g. ``LLMClient.usage``).

        ``llm_cost_usd_today`` reads ``usage.cost_usd``. The "today" framing is
        accurate for the common pattern where a scheduled agent constructs a
        fresh client per daily run; long-lived processes can instead register a
        custom provider that resets at midnight. Kept simple on purpose.
        """
        self._llm_usage = usage

    def llm_cost_usd_today(self) -> float:
        if self._llm_usage is None:
            return 0.0
        return float(getattr(self._llm_usage, "cost_usd", 0.0) or 0.0)

    def cost_by_model_today(self) -> dict[str, float]:
        """Today's spend split per model id (``{model: usd}``), or ``{}``.

        Reads ``usage.by_model`` (populated by ``LLMClient``); the same "today"
        framing as :meth:`llm_cost_usd_today`. Lets the dashboard stack daily
        cost by model from a single consistent source."""
        if self._llm_usage is None:
            return {}
        raw = getattr(self._llm_usage, "by_model", None) or {}
        return {str(m): round(float(v or 0.0), 6) for m, v in raw.items()}

    # --- snapshot -----------------------------------------------------------
    async def snapshot(self) -> dict[str, Any]:
        custom: dict[str, MetricValue] = {}
        for name, provider in self._custom.items():
            custom[name] = await _resolve(provider)
        last_run = await _resolve(self._last_run_provider) if self._last_run_provider else None
        return {
            "agent": self.agent_name,
            "uptime_s": round(self.uptime_s(), 3),
            "error_rate_5m": round(self.error_rate_5m(), 6),
            "requests_5m": self.requests_5m(),
            "errors_5m": self.errors_5m(),
            "last_run": last_run,
            "llm_cost_usd_today": round(self.llm_cost_usd_today(), 6),
            "cost_by_model_today": self.cost_by_model_today(),
            "started_at": self.started_at,
            "custom": custom,
        }


async def _resolve(provider: Callable[[], Any] | None) -> Any:
    if provider is None:
        return None
    result = provider()
    if hasattr(result, "__await__"):
        return await result
    return result
