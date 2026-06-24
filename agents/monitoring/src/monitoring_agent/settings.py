"""Agent-specific settings extending the shared base.

Adds the monitoring knobs: how often the health sweep runs, which targets it
probes, and how long an incident dedup window stays "warm" before a repeat is
treated as a fresh alert.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from agentkit import BaseAgentSettings
from pydantic import AliasChoices, Field


# agentkit ships no py.typed stubs, so BaseAgentSettings resolves to Any and
# mypy --strict flags subclassing it; the gap is upstream, not in this file.
class Settings(BaseAgentSettings):  # type: ignore[misc]  # untyped agentkit base
    agent_name: str = "monitoring-agent"
    # Postgres database created by infra/bootstrap.sql (NOT the agent_name, which
    # would resolve to a non-existent "monitoring-agent" DB).
    postgres_db: str = "monitoring"
    # Distinct Redis logical DB so keys never collide with sibling agents.
    redis_db: int = 2
    # Default in-cluster server port (host binds 127.0.0.1:8002 -> 8000).
    port: int = 8000

    # --- webhook ingress auth (ARCH-001 / AF-01) ----------------------------
    # Shared signing secret for inbound /webhook/* routes. Empty => the webhook
    # routes reject every request (fail-closed); set a strong value in prod and
    # configure the provider to HMAC-SHA256-sign the raw body with it.
    webhook_secret: str = Field("", repr=False)
    # Hard caps on untrusted webhook ingress (AF-08 unbounded-consumption).
    # Max raw request body bytes accepted on a webhook route (413 above this).
    webhook_max_body_bytes: int = 256 * 1024
    # Max number of alerts processed from one Alertmanager webhook (one LLM
    # classify call each); extras beyond this are dropped.
    webhook_max_alerts: int = 50

    # --- triage prompt branding (AF-13) -------------------------------------
    # The user-facing product the SRE-triage prompt reasons about. Generic by
    # default; a deployment sets MONITORING_BRAND to its own product name so the
    # classification prompt names the right service without a code edit.
    brand: str = Field(
        "the monitored service",
        validation_alias=AliasChoices("monitoring_brand", "brand"),
    )

    # --- health sweep -------------------------------------------------------
    # APScheduler interval for the periodic HTTP probe of the public surface.
    poll_interval_seconds: int = 60
    # URLs to probe each sweep. Override via TARGETS as JSON list in .env.
    targets: list[str] = Field(
        default_factory=lambda: ["https://example.com", "https://app.example.com"]
    )
    # HTTP probe is "slow" above this latency (seconds) -> warning.
    slow_threshold_seconds: float = 2.0
    # TLS cert expiring within this many days -> warning.
    cert_warn_days: int = 14
    # Per-probe HTTP timeout (seconds).
    probe_timeout_seconds: float = 10.0

    # --- dedup --------------------------------------------------------------
    # A repeat incident with the same dedup_key seen within this window is
    # suppressed (count incremented, no new alert fan-out).
    dedup_window_minutes: int = 30

    # --- notify reliability -------------------------------------------------
    # Bounded retry around the RabbitMQ alert publish so a transient broker
    # blip does not silently drop a page. After the last attempt fails the
    # incident is marked alert_failed (status) so the next sweep re-attempts.
    notify_max_attempts: int = 3
    # Fixed backoff between publish attempts (seconds); kept short — the sweep
    # loop is the real safety net, this just rides out a momentary blip.
    notify_retry_backoff_seconds: float = 0.5

    # --- scheduler concurrency ----------------------------------------------
    # Cap on simultaneous in-flight HTTP probes during a health sweep so the
    # target list growing does not open N sockets at once. <=0 => unbounded.
    sweep_concurrency: int = 8

    # --- retention ----------------------------------------------------------
    # Periodic prune of the unbounded time-series tables. A resolved incident
    # older than this (by last_seen) and a probe-state row not updated within
    # this window are deleted. 0 disables that prune.
    incident_retention_days: int = 90
    probe_state_retention_days: int = 30
    # APScheduler cadence for the retention prune job (default daily).
    retention_prune_interval_seconds: int = 86400

    # --- meta-monitoring: watch the agents + infra + self (Plan 2) -----------
    # Toggle + cadence; the meta sweep can run slower than the prod health sweep.
    meta_enabled: bool = True
    meta_poll_interval_seconds: int = 120

    # Agents to scrape over loopback for /healthz + /readyz.
    # NB: do NOT scrape self for liveness — a dead process can't report it's
    # dead (Plan 2 §5). Self-liveness is the external dead-man's-switch's job.
    agent_endpoints: dict[str, str] = Field(
        # The security scanner and the web-extension control plane are reachable
        # by container name on the shared ``agent_backend`` network.
        default_factory=lambda: {
            "security-agent": "http://security-agent:8000",
            "web_ext_control": "http://web_ext_control:8000",
        }
    )
    # Read-only docker-socket-proxy (GET verbs only; NEVER the raw socket).
    docker_proxy_url: str = "http://docker-socket-proxy:2375"
    watched_containers: list[str] = Field(
        default_factory=lambda: [
            "monitoring_agent",
            "security-agent",
            "web_ext_control",
            "agent-postgres-1",
            "agent-redis-1",
            "agent-rabbitmq-1",
        ]
    )
    # Cross-DB heartbeat sources: agent -> {db, table, job, interval_s}.
    # interval_s == 0 means on-demand (no overdue rule, only status failures).
    heartbeat_sources: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {
            "security-agent": {
                "db": "security",
                "table": "run_heartbeats",
                "job": "scan",
                "interval_s": 86400,
            },
            "web-ext-pipeline": {
                "db": "web_ext_pipeline",
                "table": "run_heartbeats",
                "job": "pipeline",
                "interval_s": 0,
            },
        }
    )
    # RabbitMQ management API for unconsumed-queue-depth scraping.
    rabbit_mgmt_url: str = "http://agent-rabbitmq-1:15672"
    watched_queues: list[str] = Field(default_factory=lambda: ["agent.alerts.telegram"])
    queue_depth_threshold: int = 100

    # Thresholds.
    restart_loop_threshold: int = 3  # RestartCount delta -> crash-loop
    error_spike_threshold: int = 5  # reserved for log-tail error-rate rule
    fail_streak_to_alert: int = 2  # consecutive /healthz fails before paging
    meta_probe_timeout_seconds: float = 5.0

    # Host vitals (Plan 0 §3 upgrade trigger: avail < 1.5 GB / swap > 512 MiB).
    host_meminfo_path: str = "/proc/meminfo"
    host_mem_avail_warn_mb: int = 1536
    host_swap_used_warn_mb: int = 512


@lru_cache
def get_settings() -> Settings:
    return Settings()
