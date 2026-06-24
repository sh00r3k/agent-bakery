"""agentkit — shared toolkit for self-hosted LangGraph agents.

Each agent (e.g. the monitoring agent) is a *separate package* but imports this
toolkit to share one contract for:

- config      : env-driven settings layered on shared infra (Postgres/Redis/LLM gateway)
- llm         : OpenAI-compatible chat client (-> gateway) + Ollama embeddings, cost-metered
- observability: structlog JSON logging + optional OpenTelemetry (kept lightweight)
- server      : FastAPI app factory with /healthz, /readyz, error handling
- auth        : HS256 JWT verification -> Principal (host issues tokens, agents never log in)
- db          : async psycopg pool + redis client (per-agent DB/schema on shared cluster)
- heartbeat   : run-heartbeat table for scheduled/batch agents (cross-DB monitored)
- metrics     : in-process rolling counters + the /metrics.json snapshot
- notify      : RabbitMQ alert publisher (-> chat microservice)
- prompts     : untrusted-text fencing markers + tolerant JSON extraction for LLM I/O
- egress / audit / web : private-mode egress guard, audit log, FastAPI auth adapters

No paid LangGraph Server: each agent embeds the OSS LangGraph library inside its
own FastAPI/CLI process and plugs into a shared infra layer (Postgres/Redis/
RabbitMQ) you self-host.
"""

from agentkit.auth import (
    AuthError,
    Principal,
    verify_token,
    verify_webhook_signature,
)
from agentkit.config import BaseAgentSettings
from agentkit.heartbeat import beat, create_heartbeat_table, last_beat
from agentkit.llm import LLMClient, ToolCall, ToolTurn, Usage
from agentkit.metrics import MetricsRegistry, RollingCounter
from agentkit.notify import Alert, NotifyPool, publish_alert
from agentkit.observability import get_logger, setup_observability
from agentkit.prompts import (
    SIGNAL_CLOSE,
    SIGNAL_OPEN,
    extract_json,
    fence_untrusted,
)
from agentkit.server import create_app

__all__ = [
    "SIGNAL_CLOSE",
    "SIGNAL_OPEN",
    "Alert",
    "AuthError",
    "BaseAgentSettings",
    "LLMClient",
    "MetricsRegistry",
    "NotifyPool",
    "Principal",
    "RollingCounter",
    "ToolCall",
    "ToolTurn",
    "Usage",
    "beat",
    "create_app",
    "create_heartbeat_table",
    "extract_json",
    "fence_untrusted",
    "get_logger",
    "last_beat",
    "publish_alert",
    "setup_observability",
    "verify_token",
    "verify_webhook_signature",
]

__version__ = "0.1.0"
