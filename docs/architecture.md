# Architecture — agent-bakery

**Date:** 2026-06-15

Self-hosted LangGraph agents sharing one infra layer and one Python toolkit (`agentkit`). LangGraph runs as the **OSS library embedded in each agent's process** — not the paid Platform/Server ([ADR-0001](adr/decisions.md)).

Behavior → [user-stories.md](user-stories.md) (US-NNN); rules → [business-rules.md](business-rules.md) (BR-NNN); entities → [domain-model.md](domain-model.md).

## Goals

1. **OSS, $0, no lock-in** — LangGraph as a library; no license key, no managed runtime ([ADR-0001](adr/decisions.md)).
2. **Independently deployable agents on a thin seam** — each agent/app is its own workspace member; only shared code is `agentkit` (US-012).
3. **Self-observing** — every agent exposes `/healthz` `/readyz` `/metrics.json`; a meta-monitor watches and alerts (US-007, US-011).
4. **Cost is bounded** — every LLM call is metered against a per-request USD ceiling; multi-step jobs carry a per-job cap (BR-006, BR-007).
5. **Multi-tenant by construction** — per-tenant isolation everywhere; one explicit `ops` exception (BR-002).

### Non-goals

- ❌ LangGraph Server/Platform/Studio, license keys, `RemoteGraph` (NS-003).
- ❌ A platform-run end-user login / identity store — tenants own identity (NS-002).
- ❌ Cross-agent table access — reads are HTTP/AMQP only (AR-3).
- ❌ Always-on tracing — OpenTelemetry optional, off by default.

---

## Layers

**Rule:** a layer depends only on layers below — never up, never sideways into a peer agent's internals.

| Layer            | Responsibility                                                                                                                  | Depends on |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| 1. Shared infra  | Postgres+pgvector, Redis, RabbitMQ, Ollama embeddings, OpenAI-compatible LLM gateway, Caddy edge, read-only docker-socket-proxy | —          |
| 2. `agentkit`    | One contract for config, LLM, observability, server, auth, db, notify                                               | Layer 1    |
| 3. Agents & apps | `agents/monitoring`, `apps/dashboard`, `apps/platform-cli`, `agents/ultraqa`, `agents/<yours>` — each own deploy                 | Layer 2    |

### Layer 1 — shared infra (docker network `agent_backend`)

- `agent-postgres-1` (pgvector) — one database/schema per agent; bootstrap in `infra/`.
- `agent-redis-1` — one logical DB index per agent.
- `agent-rabbitmq-1` — alert/event bus; agents publish topic `agent.alerts`, notification microservice consumes (AR-5).
- `ollama` (`nomic-embed-text`) — free local embeddings.
- **chat-LLM gateway** (OpenAI-compatible) — LiteLLM / vLLM / Ollama / OpenAI via `LLM_BASE_URL` (`https://your-gateway.example.com/v1`).
- **Caddy** — opt-in `edge` profile: TLS + per-agent subdomain routing (see [deploy-your-own.md](deploy-your-own.md)).
- **docker-socket-proxy** — read-only Docker state for monitoring (never the raw socket).

### Layer 2 — `agentkit` (`packages/agentkit`)

The thin seam imported by every member. Business logic never lives here.

| Module          | Responsibility                                                              | Public surface                              |
| --------------- | -------------------------------------------------------------------------- | ------------------------------------------- |
| `config`        | env contract on infra defaults; subclass per agent                         | `BaseAgentSettings`                         |
| `llm`           | OpenAI-compatible chat + Ollama embeddings; USD cost meter + ceiling (BR-006); tool-calling | `LLMClient`, `CostCeilingExceeded` |
| `observability` | structlog JSON; optional OpenTelemetry (off); secret/PII redaction         | logger, tracer, `setup_observability()`     |
| `server`        | FastAPI factory: `/healthz`, `/readyz`, `/metrics.json`, error + metrics mw | `create_app()`                             |
| `auth`          | HS256 JWT → `Principal` (role + tenant); verifies (SR-4)                   | `Principal`, `verify_token`                 |
| `db`            | async psycopg pool + redis client                                          | `pg_pool()`, `redis_client()`              |
| `notify`        | publish alert to RabbitMQ `agent.alerts` → notification microservice (AR-5) | `publish_alert()`                          |
| `egress` / `audit` | private-mode egress guard; shared audit-log primitive                   | `guard()`, `record()`                       |

Minimal member contract (US-012):

```python
from agentkit import BaseAgentSettings, create_app

class Settings(BaseAgentSettings):
    agent_name: str = "my-agent"

app = create_app(title="my-agent")   # gets /healthz /readyz /metrics.json for free
```

### Layer 3 — agents & apps (workspace members, each its own deploy)

| Member              | Kind                                                              | Role                                                |
| ------------------- | ---------------------------------------------------------------- | --------------------------------------------------- |
| `agents/monitoring` | scheduled (agentkit-based)                                       | meta-monitor → Signals → Incidents → alerts         |
| `apps/dashboard`    | request/response (agentkit-based, HTMX)                          | config-driven ops console, HTTP fan-out across agents |
| `apps/platform-cli` | CLI (`platform` console script)                                  | owns the `DASHBOARD_AGENTS` registry key; compose lifecycle, token mint, health-probe |
| `agents/ultraqa`    | scheduled + on-demand (agentkit-based, ReAct + MCP browser)      | autonomous QA tester against a dev SUT (Phase 2; ADR-0008) |
| `agents/<yours>`    | anything on agentkit                                            | a new agent that gets the contract for free          |

---

## Member: `agents/monitoring`

agentkit-based, scheduled (APScheduler tick). The meta-monitor. `collect → evaluate → classify → dedup → notify`:

- **Collect** each agent's `/healthz`+`/readyz`+`/metrics.json`, Docker container state (read-only socket-proxy), host vitals, RabbitMQ queue depth.
- **Evaluate** the fixed SLO rule set — `agent-down`, `error-spike`, `batch-overdue` — emitting a `Signal` with deterministic `dedup_key = (rule, target)` + severity (BR-008).
- **Dedup** Signals into Incidents: at most one `firing` Incident per `dedup_key`; alert **once** per firing transition; recurrences bump `last_seen_at`/`signal_count` without re-alerting (BR-009).
- **Notify** via agentkit `notify` → RabbitMQ `agent.alerts` (AR-5).

## Member: `apps/dashboard`

agentkit-based HTMX ops console. **Config-driven agent registry** — runs against any composition of agents; absent-agent panels are skipped, not errored (US-013). Reads agents over HTTP via a freshly minted `ops` JWT (agentkit `auth`).

**Panels** — agent health (from monitoring), open incidents, ultraQA findings (severity-filtered, US-017), LLM cost, plus the workspace/activity/privacy UI pages.

---

## Data flows

**Flow A — agent monitoring → alert**

```
APScheduler tick → collect(/healthz,/readyz,/metrics.json, docker-socket-proxy,
                           host vitals, rabbitmq depth)
   → evaluate SLO rules → Signal(rule,target,severity,dedup_key)  [BR-008/US-011]
   → dedup into firing Incident (one per dedup_key)               [BR-009]
   → notify ONCE → RabbitMQ agent.alerts → notification microservice (AR-5)
```

**Flow B — dashboard fan-out**

```
ops → dashboard (HTMX) → mint ops JWT → HTTP GET each registered agent
   → monitoring: health/incidents   → ultraqa: findings
   → render only present-agent panels (registry-driven)  [US-007/US-013]
```

**Flow C — ultraQA sweep (Phase 2)**

```
APScheduler tick | POST /scan → ReAct loop (LLMClient.complete_with_tools)
   → every SUT call traverses the egress guard (BR-012, env=dev BR-011)
   → record CoverageNodes; defects → upsert Finding by dedup_key (BR-015)
   → critical Finding → ONE Alert → RabbitMQ agent.alerts (BR-009/US-017)
```

---

## Storage / persistence

Per-agent isolation (AR-3): each agent owns its database/schema; no cross-agent table access. An agent that needs LangGraph state persistence wires its own checkpointer against that agent's Postgres.

| Member     | Key tables (see [domain-model.md](domain-model.md))                | Notes                                                  |
| ---------- | ------------------------------------------------------------------- | ------------------------------------------------------ |
| monitoring | `signals`, `incidents`                                              | one `firing` Incident per `dedup_key` (BR-009)         |
| ultraqa    | `qa_runs`, `qa_steps`, `findings`, `coverage_nodes` (pgvector)      | `findings` unique by `dedup_key` (BR-015)              |
| dashboard  | none of its own                                                     | reads agents over HTTP (Flow B)                        |

Redis: one logical index per agent (caches, rate windows).

---

## Multi-tenancy

`tenant_id` is bound from the verified `Principal` at the repository layer — never from request input (CR-1). `end-user`/`operator` are confined to one tenant; `ops` is the only documented cross-tenant identity (BR-002). Secret values are never serialized to a response or log (SR-4, BR-010).

---

## Observability

- **Logs:** structlog JSON to stdout; named domain events are the contract the monitor/verification read — `llm.call`, `signal.emitted`, `incident.opened`, `incident.alerted`, `incident.resolved`, `run.started`, `run.finished`, `finding.upserted`.
- **Metrics:** `/metrics.json` rolling counters (requests, errors, LLM cost) — scraped by monitoring (Flow A).
- **Health:** `/healthz` (process alive) vs `/readyz` (deps reachable) — drives graceful degradation + `agent-down` Signals.
- **Tracing:** OpenTelemetry optional, off by default; raw content is a debug-only opt-in (SR-5).

---

## Security

- **Secrets:** `env.example` only; real values from env via `BaseAgentSettings`; secret-scan in CI; zero business identifiers / host IPs in a public repo (SR-1, SR-2, BR-010).
- **AuthN/Z:** agents verify HS256 JWT → `Principal` (agentkit `auth`); dashboard mints `ops` JWTs; no end-user login (SR-4, NS-002).
- **Autonomous egress (ultraQA):** every outbound SUT call traverses one fail-closed guard; env=dev only, non-admin creds, read-only DB (BR-011/012/013).
- **LLM cost:** per-request ceiling + per-job cap, aborted/truncated on breach (BR-006/007).
- **Notifications:** only via RabbitMQ `agent.alerts`; no direct chat-platform calls (AR-5).

---

## Performance & scaling

- Each member scales independently (own process/container); blast radius isolated (ADR-0001).
- Embeddings local (Ollama, free); chat LLM cost bounded per request/job.
- Monitoring runs on a scheduler tick, not per-request; dashboard fans out read-only.
- Single-host today; outgrowing one host or needing Studio-grade debugging is the documented revisit trigger for ADR-0001.

---

## Versioning policy

| Change                                                                           | Bump              |
| -------------------------------------------------------------------------------- | ----------------- |
| Bug fix, no contract change                                                      | Patch             |
| New optional field / new agentkit export / new agent                             | Minor             |
| Removed/renamed agentkit export, changed agent HTTP contract, changed event name | **Major**         |
| Changed default tenancy/cost behavior                                            | **Major** (+ ADR) |

---

## Open questions (ADR candidates)

1. Multi-host / horizontal scale-out of a single agent — when, and how does the monitor target it?
2. ultraQA coverage model — how exhaustively should a sweep walk the SUT before it's "enough"?

---

## Related

- [constitution.md](constitution.md) — iron rules
- [principles.md](principles.md) — engineering principles
- [domain-model.md](domain-model.md) · [user-stories.md](user-stories.md) · [business-rules.md](business-rules.md) · [functional-decomposition.md](functional-decomposition.md)
- [adr/decisions.md](adr/decisions.md)
- [deploy-your-own.md](deploy-your-own.md) · [add-your-own-agent.md](add-your-own-agent.md)
