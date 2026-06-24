# Public API Reference — agent-bakery

**Date:** 2026-06-15
**Version:** v0.1 (skeleton — tracks the contract as the agents are built)
**Status:** Public contract. Two surfaces are stable-by-intent: (1) the **agentkit
Python API** every agent imports, and (2) the **monitoring + dashboard HTTP API** the
dashboard and operators consume. Breaking either is a major version bump.

---

## What counts as "public API"

1. **agentkit Python exports** — `config`, `llm`, `observability`, `server`, `auth`,
   `db`, `notify`. Consumed by every agent + the dashboard.
2. **monitoring + dashboard surfaces** — monitoring's health/metrics + read API; the
   dashboard's HTMX panels and its HTTP fan-out to agents; ultraQA's `/findings` + `/scan`.
3. **The JWT token contract** — HS256 tokens the **host mints** and agents **verify**
   (`auth.verify_token` → `Principal`), plus the dashboard-minted **ops token**.

Generic tenants: `acme`, `demo`. The LLM gateway is `https://your-gateway.example.com/v1`,
always from env (`LLM_BASE_URL`). No real secret/key/host appears here (BR-010).
Cross-refs: `US-NNN` ([user-stories.md](user-stories.md)), `BR-NNN`
([business-rules.md](business-rules.md)), `ADR-NNNN` ([adr/decisions.md](adr/decisions.md)).

---

# Part 1 — agentkit Python API

Import surface (`packages/agentkit`):

```python
from agentkit import BaseAgentSettings, LLMClient, create_app
from agentkit.auth import verify_token, Principal
from agentkit.db import pg_pool, redis_client
from agentkit.notify import publish_alert
```

## `config.BaseAgentSettings`

Env-driven Pydantic settings on the shared infra; subclass per agent.

| Field                  | Env var                | Type   | Default                               | Notes                                     |
| ---------------------- | ---------------------- | ------ | ------------------------------------- | ----------------------------------------- |
| `agent_name`           | `AGENT_NAME`           | str    | (required)                            | Identifies the agent to monitoring + logs |
| `database_url`         | `DATABASE_URL`         | str    | (required)                            | Per-agent Postgres DB (pgvector)          |
| `redis_url`            | `REDIS_URL`            | str    | (required)                            | Per-agent logical Redis index             |
| `rabbitmq_url`         | `RABBITMQ_URL`         | str    | —                                     | Alert/event bus; optional if no notify    |
| `llm_base_url`         | `LLM_BASE_URL`         | str    | `https://your-gateway.example.com/v1` | OpenAI-compatible gateway                 |
| `llm_api_key`          | `LLM_API_KEY`          | secret | —                                     | Gateway key (never logged)                |
| `llm_model`            | `LLM_MODEL`            | str    | gateway default                       | Chat model id                             |
| `embed_base_url`       | `EMBED_BASE_URL`       | str    | local Ollama                          | Embeddings endpoint                       |
| `embed_model`          | `EMBED_MODEL`          | str    | `nomic-embed-text`                    | 768-dim embeddings                        |
| `llm_max_cost_usd`     | `LLM_MAX_COST_USD`     | float  | `0.10`                                | Per-request ceiling (BR-006)              |
| `jwt_secret`           | `JWT_SECRET`           | secret | (required)                            | HS256 verify secret (never logged)        |
| `private_mode`         | `PRIVATE_MODE`         | bool   | `false`                               | Blocks outbound TCP except an allow-list; skips OpenTelemetry |
| `otel_exporter_otlp_endpoint` | `OTEL_EXPORTER_OTLP_ENDPOINT` | str | `""`                       | OTLP endpoint; empty = tracing off        |
| `log_level`            | `LOG_LEVEL`            | str    | `INFO`                                | structlog level                           |

Secrets load from env only (`env.example` shipped, BR-010).

## `llm.LLMClient`

OpenAI-compatible chat + Ollama embeddings, with a USD cost meter and per-request ceiling (BR-006).

```python
class LLMClient:
    def __init__(self, settings: BaseAgentSettings): ...

    async def chat(
        self,
        messages: list[dict],          # OpenAI message format
        *,
        model: str | None = None,
        response_format: dict | None = None,  # structured/JSON output
        max_tokens: int | None = None,
    ) -> ChatResult: ...

    async def complete_with_tools(
        self, messages: list[dict], *, tools: list[dict], **kw
    ) -> ToolTurn: ...                 # tool_calls + content + finish_reason (US-020)

    async def embed(self, texts: list[str]) -> list[list[float]]: ...  # 768-dim

    @property
    def cost_usd(self) -> float: ...   # cumulative metered spend on this client
```

`ChatResult` = `{ text: str, tokens_in: int, tokens_out: int, cost_usd: float, model: str }`.
A call whose metered cost would exceed `settings.llm_max_cost_usd` raises
`CostCeilingExceeded` (BR-006). Every call emits a `llm.call` log event with `cost_usd`.

## `server.create_app`

FastAPI factory; mounts the contract endpoints + structured error handling + a metrics middleware.

```python
def create_app(
    settings: BaseAgentSettings,
    *,
    title: str | None = None,
    lifespan=None,                      # optional async context (open pools, build graph)
    metrics_public: bool = False,       # expose /metrics.json without auth
) -> FastAPI: ...
```

Agents mount their own `APIRouter`s on the returned `app` after construction. Always-mounted
endpoints (the contract monitoring scrapes — US-011, US-012):

| Method | Path            | Auth | Response                                                         |
| ------ | --------------- | ---- | ---------------------------------------------------------------- |
| GET    | `/healthz`      | none | `200 {"status":"ok"}` — process is alive                         |
| GET    | `/readyz`       | none | `200 {"status":"ready"}` or `503` — deps (DB/Redis) reachable    |
| GET    | `/metrics.json` | none | rolling counters: `{requests, errors, llm_calls, cost_usd, ...}` |

Unhandled exceptions become a uniform JSON envelope `{"error": {"code": str, "message": str}}`
with the right status; never leaks stack traces or secrets. The middleware feeds
`/metrics.json` and the `error-spike` SLO rule (BR-008).

## `auth.verify_token` → `Principal`

The host mints HS256 JWTs; agents only verify (NS-002 — no login here).

```python
@dataclass(frozen=True)
class Principal:
    sub: str              # JWT "sub" — opaque user/ops id
    tenant: str           # JWT "tenant" — tenant slug, e.g. "acme"
    role: str             # "end-user" | "operator" | "ops"
    name: str | None = None

def verify_token(
    token: str, *, secret: str, algorithms: list[str], audience: str | None
) -> Principal: ...
# raises AuthError(401) on bad signature / expiry / missing claims
```

Claims (Part 3): `{tenant, sub, role, exp}`, HS256, signed with `JWT_SECRET`.
Isolation (BR-002): an `end-user`/`operator` Principal is confined to `principal.tenant`;
only `role="ops"` may cross tenants.

## `db`

```python
def pg_pool(settings) -> AsyncConnectionPool: ...           # async psycopg pool (async ctx mgr)
def redis_client(settings) -> Redis: ...                    # redis client
```

An agent that needs LangGraph state persistence wires its own checkpointer against the
pool from `pg_pool`, so a `StateGraph` persists/resumes its state per `thread_id`.

## `notify`

```python
async def publish_alert(rabbitmq_url: str, alert: Alert) -> None: ...
# publishes to RabbitMQ topic "agent.alerts"; a notification microservice
# consumes and fans out (e.g. to a chat platform). Agents NEVER call a chat
# API directly (architecture convention).
```

`Alert` shape: `{ "agent": str, "rule": str, "target": str, "severity": "info|warning|critical",
"dedup_key": str, "summary": str, "detail": object }`. Used by monitoring (US-011, BR-009)
and ultraQA (critical findings, US-017).

---

# Part 2 — monitoring HTTP surface

The monitoring agent is agentkit-based and mostly **scheduled** (collector ticks), so its
HTTP surface is the agentkit contract plus a small read API the dashboard consumes for the
health and incidents panels (US-007, US-011).

| Method | Path                                 | Auth  | Notes                                                                          |
| ------ | ------------------------------------ | ----- | ------------------------------------------------------------------------------ |
| GET    | `/healthz` `/readyz` `/metrics.json` | none  | agentkit contract                                                              |
| GET    | `/agents`                            | `ops` | Per-agent last-seen health: `{agent, healthy, ready, last_scrape_at, metrics}` |
| GET    | `/incidents`                         | `ops` | Currently `firing` Incidents (+ optional `?status=resolved`)                   |
| GET    | `/incidents/{id}`                    | `ops` | One Incident with its folded Signals                                           |

**`GET /incidents` response:**

```json
[
  {
    "id": "c1..",
    "rule": "agent-down",
    "target": "example-agent",
    "severity": "critical",
    "status": "firing",
    "signal_count": 5,
    "first_seen_at": "2026-06-15T09:00:00Z",
    "last_seen_at": "2026-06-15T09:20:00Z",
    "notified_at": "2026-06-15T09:00:05Z"
  }
]
```

Collectors (no HTTP, scheduled): scrape each agent's `/healthz` + `/readyz` + `/metrics.json`,
Docker state over a **read-only socket-proxy**, host vitals, and RabbitMQ queue depth → SLO
rules (BR-008) → Signals → dedup → one Incident per `dedup_key` → at most one alert to
RabbitMQ `agent.alerts` (BR-009, via agentkit `notify`).

---

# Part 3 — ultraQA HTTP surface (Phase 2)

agentkit-based, scheduled + on-demand (ADR-0008). Beyond the agentkit contract:

| Method | Path         | Auth  | Notes                                                                     |
| ------ | ------------ | ----- | ------------------------------------------------------------------------- |
| GET    | `/findings`  | `ops` | Deduped Findings (`?severity=`), for the dashboard `features:["findings"]` (US-017) |
| POST   | `/scan`      | `ops` | Trigger an on-demand sweep; same behavior as the scheduled tick (US-019)  |

`/metrics.json` also carries `custom.coverage_pct` = explored / (explored+unexplored) (US-018).
Every outbound SUT call traverses the fail-closed egress guard (BR-011/012/013).

---

# Part 4 — dashboard surface

HTMX ops console (agentkit-based). It owns **no domain data**: it reads agents over HTTP
using a freshly minted **ops JWT** (Part 5) and renders server-side HTMX panels. Config-driven
agent registry — runs against any composition of agents (US-013): panels for absent agents are
skipped, not errored.

| Path             | Renders                                  | Reads from                | Story          |
| ---------------- | ---------------------------------------- | ------------------------- | -------------- |
| `/`              | Agent health                             | monitoring `/agents`      | US-007         |
| `/incidents`     | Open/recent incidents                    | monitoring `/incidents`   | US-007, US-011 |
| `/findings`      | ultraQA findings (severity-filtered)     | ultraqa `/findings`       | US-017         |

Every fan-out call presents a freshly minted `ops` JWT (US-013). The dashboard itself sits
behind the proxy / its own ops login (out of scope here — an internal console, not a
tenant-facing surface).

---

# Part 5 — Token contract (HS256)

The host (or the dashboard, for ops) mints JWTs; **agents only verify** (agentkit
`auth.verify_token`, NS-002). One shared `JWT_SECRET` (HS256) across all agents.

## Tenant token (host-minted)

For multi-tenant agents, the host mints a tenant-scoped token:

```json
{
  "tenant": "acme", // tenant slug  → Principal.tenant
  "sub": "u-12", // opaque user id → Principal.sub
  "role": "end-user", // or "operator" → Principal.role
  "exp": 1750000000 // unix expiry; short-lived (minutes)
}
```

- HS256, signed with `JWT_SECRET`.
- `role="end-user"`/`"operator"` are confined to the `tenant` claim — never cross tenants (BR-002).

## Ops token (dashboard-minted)

Minted per fan-out call:

```json
{
  "tenant": "agent-bakery", // marks a platform-level (non-tenant) principal
  "sub": "dashboard", // ops actor id
  "role": "ops", // → Principal.role = "ops"
  "exp": 1750000060 // very short-lived (per request)
}
```

- `role="ops"` is the **only** role allowed to cross tenants (BR-002); it reads
  `/agents`, `/incidents`, `/findings` across every registered agent.
- Minted with the same agentkit `auth` helper and verified identically.

**Verification failures** (all surfaces): bad signature, expiry, missing claims, or a role
attempting a forbidden cross-tenant action → `401`/`403` with the uniform envelope; no data
is read or written.

---

## Error envelope (all HTTP surfaces)

```json
{ "error": { "code": "string", "message": "human-readable" } }
```

| Status | When                                                                             |
| ------ | -------------------------------------------------------------------------------- |
| `400`  | Malformed body / invalid parameters                                              |
| `401`  | Missing/invalid JWT                                                              |
| `403`  | Authenticated but forbidden (e.g. operator crossing tenants — BR-002)            |
| `404`  | Resource not found _within the caller's scope_                                   |
| `429`  | Rate-limited                                                                     |
| `500`  | Unhandled — uniform envelope, never a stack trace or secret                      |

---

## API Stability

- **v0.x** — contract may shift during the skeleton phase.
- **v1.0** — stable; breaking changes to the agentkit exports or HTTP shapes require
  a major bump + an ADR.
- Deprecated items get a deprecation note + removal in the next major.

---

## Related docs

- [domain-model.md](domain-model.md) — entities behind these shapes
- [user-stories.md](user-stories.md) — US-NNN behavior referenced above
- [business-rules.md](business-rules.md) — BR-NNN invariants enforced here
- [architecture.md](architecture.md) — where each surface physically lives
- [adr/decisions.md](adr/decisions.md) — library-not-platform
- [packages/agentkit/README.md](../packages/agentkit/README.md) — agentkit module overview
