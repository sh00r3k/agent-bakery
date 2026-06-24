# Vision — agent-bakery

**Date:** 2026-06-15
**Version:** v1.0
**Scope:** the whole monorepo.

## Why this exists

Running a few LangGraph agents in production forks two ways:

- **OSS library** — free, but you build all plumbing yourself (config, cost-controlled LLM client, health/metrics, JWT auth, Postgres/Redis/RabbitMQ, state persistence, alerting, ops view); each agent reimplements it and drifts.
- **LangGraph Server/Platform** — managed but commercial: license key, LangSmith key, startup validation, vendor beacon egress.

**agent-bakery is the third path: self-hosted OSS LangGraph agents sharing one infrastructure layer and one Python toolkit (`agentkit`).** OSS embedded in each agent's process (free, no keys, no egress); `agentkit` removes per-agent boilerplate so agents look the same and are swappable. Rationale: [ADR-0001](adr/decisions.md). The repo ships a meta-monitor, an ops dashboard, an operator CLI, an example agent, and an agentic QA tester (ultraQA) — all on `agentkit`.

## Who it's for

A small platform/infra team (1–5 engineers) that:

- self-hosts (Postgres, Redis, reverse proxy) and wants agents on the same boxes, not a SaaS;
- needs more than one agent and refuses to copy-paste boilerplate;
- is cost-sensitive and wants a hard per-request USD ceiling;
- wants OSS, no license keys, no lock-in (Apache-2.0), bringing its own OpenAI-compatible gateway (LiteLLM / vLLM / Ollama / OpenAI) and local Ollama embeddings;
- treats observability and alerting as table stakes.

Not a managed agent cloud — it buys back the weeks to make a LangGraph agent production-grade, once.

## What changes after adopting it

- New agent in ~40 lines (`examples/hello-agent`): subclass `BaseAgentSettings`, build a `StateGraph`, call `create_app()` — health, metrics, auth, persistence, cost-metered LLM come free (US-012).
- One shared infra layer (one Postgres with a DB per agent, one Redis, one RabbitMQ, one gateway, one Ollama) instead of N stacks ([architecture.md](architecture.md)).
- Cross-agent health and incidents in one ops console against any composition of agents (US-007, US-013).
- Per-tenant isolation and a hard per-request LLM cost ceiling, baked into the toolkit (BR-002, BR-006).

Stops: rewriting config/auth/health/LLM/persistence per agent; paying for a managed runtime; unbounded LLM cost.

## Product principles

1. **Library, not platform.** Embed OSS LangGraph; own the FastAPI surface; no license keys, no vendor egress. (ADR-0001)
2. **The toolkit is the keystone.** Everything an agent needs lives once in `agentkit`; agents stay thin and reuse, never fork.
3. **Safe autonomy.** Any outbound / tool-using agent is env-gated and passes a fail-closed egress guard; it can't touch prod or fire destructive actions (BR-011, BR-012).
4. **Isolation is non-negotiable.** One DB + one Redis index per agent; every multi-tenant agent operation is tenant-scoped; `ops` is the single explicit exception (BR-002).
5. **Cost is first-class.** Every LLM call is metered with a USD ceiling; multi-step jobs carry a cap (BR-006, BR-007).
6. **Bring your own everything.** Gateway, models, host, domain — env-driven. `env.example` only; the public repo carries no secrets or business identifiers (BR-010).
7. **Optional observability.** structlog JSON always; OpenTelemetry tracing opt-in, off by default.

## Success metrics

- **North star:** `git clone` to a new agent answering `/healthz` and scraped by the monitor — target under an afternoon (US-012).
- **Leading:** % of plumbing provided by `agentkit`; per-request LLM cost under ceiling; spec-organized tests green.
- **Lagging:** agents on one shared stack without drift; ultraQA coverage trend; mean alerts-per-incident → 1 (BR-009).

## Non-goals

- The paid LangGraph Server/Platform, Studio, a managed multi-graph runtime, or `RemoteGraph`; cross-agent calls are plain HTTP/AMQP. (ADR-0001, NS-003)
- Our own LLM gateway or orchestrator.
- An identity provider / end-user login; tenants own identity, the system verifies JWTs and never logs anyone in (NS-002).
- A managed multi-tenant SaaS — you self-host it.
- A heavy SPA dashboard — the ops console is server-rendered HTMX.

## Evolution

Changes rarely; a change here cascades down the spec chain (see [README.md](README.md)). On a major pivot, archive under `vision-archive/v0.x.md` and rewrite. Decision history lives in [`adr/decisions.md`](adr/decisions.md).

## Related docs

- [README.md](README.md) · [architecture.md](architecture.md)
- [user-stories.md](user-stories.md) (US-NNN) · [business-rules.md](business-rules.md) (BR-NNN)
- [functional-decomposition.md](functional-decomposition.md) · [domain-model.md](domain-model.md)
- [adr/decisions.md](adr/decisions.md)
