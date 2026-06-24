# agentkit

The shared toolkit every agent imports. One contract for config,
LLM access, observability, the FastAPI app factory, JWT auth, and the
Postgres/Redis/RabbitMQ plumbing.

## What it gives you

| Module          | Purpose |
|-----------------|---------|
| `config`        | `BaseAgentSettings` — env-driven settings layered on the shared infra (Postgres/Redis/RabbitMQ, an OpenAI-compatible LLM gateway, Ollama embeddings). Subclass per agent. |
| `llm`           | `LLMClient` — OpenAI-compatible chat (any gateway: LiteLLM/vLLM/Ollama/OpenAI) + embeddings, with per-request cost metering, a USD ceiling, and an in-process circuit breaker. |
| `observability` | structlog JSON logging + optional OpenTelemetry tracing (off by default; enable via `OTEL_EXPORTER_OTLP_ENDPOINT`, install the `observability` extra). |
| `server`        | `create_app()` FastAPI factory with `/healthz`, `/readyz`, structured error handling. |
| `auth`          | HS256 JWT verification → `Principal`. The host mints tokens; agents only verify. |
| `db`            | async psycopg pool + redis client. |
| `heartbeat`     | liveness rows so a meta-monitor can see whether an agent is alive. |
| `metrics` / `notify` | rolling counters + alert publishing over RabbitMQ. |
| `egress` / `audit` | private-mode egress guard + shared audit-log primitive. |

## Design

Your agents run the **OSS LangGraph library embedded in each agent's own
process** — not the paid LangGraph Platform/Server. agentkit is the seam that
keeps every agent consistent and swappable: one settings base, one LLM client,
one server factory.

```python
from agentkit import BaseAgentSettings, LLMClient, create_app

class Settings(BaseAgentSettings):
    agent_name: str = "my-agent"

app = create_app(title="my-agent")
```

Installed as a uv workspace path dependency — see the repo root README.
