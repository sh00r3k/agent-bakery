# Functional Decomposition — agent-bakery

**Date:** 2026-06-15
**Type:** Capability hierarchy — what the system _can do_.

---

## What this is

A hierarchical map of the functional surface, so any new feature has an obvious home.
Leaves link to the user stories (US-NNN) and business rules (BR-NNN) that define their
behavior. Decomposition is by **capability**, not technology.

```
System → Domain Area → Capability → Feature → Story (US-NNN) → Task
```

Status legend: ✅ implemented · 🚧 in progress · 📋 planned · 💭 idea · ❌ rejected.
Most leaves are 📋 — this is the spec foundation, not a status report.

## System: agent-bakery

Self-hosted OSS LangGraph agents sharing one infra layer and one Python toolkit
([architecture.md](architecture.md), [ADR-0001](adr/decisions.md)): a shared toolkit, a
meta-monitor, an ops dashboard, an operator CLI, and an agentic QA tester.

---

## Domain Area A: Shared toolkit (`packages/agentkit`)

Cross-agent seam giving every agent one contract. Owns no domain data.

- **A.1 Configuration & settings**
  - A.1.1 📋 `BaseAgentSettings` — env contract over shared infra (Postgres/Redis/RabbitMQ, gateway, Ollama); subclass per agent; secrets only from env. US-012 · BR-010 · agentkit `config`
- **A.2 LLM access**
  - A.2.1 📋 `LLMClient` chat over any OpenAI-compatible gateway (`LLM_BASE_URL`); tool-calling via `complete_with_tools`. US-020 · BR-006 · agentkit `llm`
  - A.2.2 📋 Ollama embeddings `vector(768)` — local `nomic-embed-text` for RAG. US-021 · agentkit `llm`
  - A.2.3 📋 USD cost meter + per-request ceiling + per-job cap. US-020 · BR-006, BR-007 · agentkit `llm`
- **A.3 Observability**
  - A.3.1 📋 structlog JSON logging (+ redaction). BR-010 · agentkit `observability`
  - A.3.2 💭 Optional OpenTelemetry tracing (off by default). agentkit `observability`
- **A.4 App factory (`create_app`)**
  - A.4.1 📋 FastAPI factory: `/healthz` `/readyz` `/metrics.json`. US-012 · agentkit `server`
  - A.4.2 📋 Error handling + metrics middleware. US-011 (error-spike source) · agentkit `server`
- **A.5 Auth**
  - A.5.1 📋 HS256 JWT → `Principal` (per-tenant). US-013 · BR-002 · agentkit `auth`
- **A.6 Persistence**
  - A.6.1 📋 async psycopg pool (`pg_pool`) + redis; an agent needing LangGraph state persistence wires its own checkpointer against this pool. agentkit `db`
- **A.7 Messaging**
  - A.7.1 📋 RabbitMQ alert publish (`agent.alerts` → notification microservice). US-011 · BR-009 · agentkit `notify`

---

## Domain Area C: Agent monitoring (`agents/monitoring`)

agentkit-based meta-monitor, own Postgres DB. Scheduled collect → triage → dedup → alert.

- **C.1 Collection**
  - C.1.1 📋 Scrape agent `/healthz` `/readyz` `/metrics.json`. US-007, US-011 · BR-008
  - C.1.2 📋 Docker state via read-only socket-proxy. US-011 · BR-008
  - C.1.3 📋 Host vitals + RabbitMQ queue depth. US-011 · BR-008
- **C.2 SLO evaluation → Signals**
  - C.2.1 📋 Rules: agent-down, error-spike, batch-overdue. US-011 · BR-008 · Entity Signal
- **C.3 Triage, dedup, alert**
  - C.3.1 📋 LLM classify severity. US-011 · BR-006 · Node classify
  - C.3.2 📋 Dedup Signals → Incident (one firing per `dedup_key`). US-011 · BR-009 · Entity Incident
  - C.3.3 📋 Alert once → RabbitMQ `agent.alerts`. US-011 · BR-009 · agentkit `notify`

---

## Domain Area D: Ops dashboard (`apps/dashboard`)

agentkit-based HTMX console. Config-driven agent registry; reads agents over HTTP with a
minted ops JWT. Owns no domain data.

- **D.1 Agent registry**
  - D.1.1 📋 Config-driven registry (runs with ANY composition). US-013 · dashboard registry
  - D.1.2 📋 HTTP fan-out with minted ops JWT. US-013 · BR-002 · agentkit `auth`
- **D.2 Panels**
  - D.2.1 📋 Agent health (reads monitoring + agent health). US-007
  - D.2.2 📋 Incidents. US-007, US-011 · Entity Incident
  - D.2.3 📋 ultraQA findings (severity-filtered). US-017 · Entity Finding

---

## Cross-cutting concerns

- **Observability:** structlog JSON (A.3.1), optional tracing (A.3.2), `/metrics.json` (A.4.1) feeding monitoring (C.1.1).
- **Security:** JWT `Principal` (A.5.1), secret hygiene (BR-010).
- **Multi-tenancy:** isolation everywhere (BR-002); `tenant_id` bound from the `Principal`.
- **Cost control:** per-request ceiling + per-job cap (A.2.3, BR-006/BR-007).

---

## Dependencies between capabilities

| Depends on ↓ / for → | A.\* toolkit | C monitoring  | D dashboard     |
| -------------------- | ------------ | ------------- | --------------- |
| A.\* (agentkit)      | —            | ✓             | ✓               |
| C (monitoring)       | —            | —             | reads (D.2.1/2) |
| D (dashboard)        | —            | —             | —               |

**Implementation order:** agentkit (A) → monitoring (C) → dashboard (D).

---

## MVP

- **Capabilities:** A.1, A.2, A.4, A.5, A.6 (toolkit baseline); US-012 (a new agent can boot).
- **Features:** A.1.1, A.2.1, A.2.3, A.4.1, A.5.1, A.6.1.
- **Deferred to Phase 1+:** all of C (monitoring), all of D (dashboard).

---

## Related docs

- [user-stories.md](user-stories.md) · [domain-model.md](domain-model.md) ·
  [business-rules.md](business-rules.md) · [architecture.md](architecture.md) ·
  [add-your-own-agent.md](add-your-own-agent.md)
