# agent-bakery

## Context loading (lazy — read on a need-to-know basis)

This guide references many `docs/*` files. Do **not** read them all upfront.
Load a referenced file with your Read tool **only when the current task touches
that area** (working in a member → read that member's docs; changing a rule →
read `business-rules.md`, etc.). Treat a loaded file as mandatory instructions.
Keep the context lean: pull spec docs on demand, not preemptively.

## Project context

A set of **self-hosted LangGraph agents** — OSS, no paid LangGraph
Server/Platform. Your agents embed the OSS LangGraph library inside each agent's
own process and shares one infrastructure layer (Postgres+pgvector / Redis /
RabbitMQ / Ollama / an OpenAI-compatible LLM gateway) and one Python toolkit
(`agentkit`). Members:

- `packages/agentkit` — shared toolkit: config, llm, observability, server, auth,
  db, notify, heartbeat, metrics. The keystone every member imports.
- `agents/monitoring` — agent meta-monitor: scrape → SLO rules → Signal → dedup →
  alert (RabbitMQ `agent.alerts`).
- `apps/dashboard` — HTMX ops console; config-driven agent registry, reads agents
  over HTTP with a minted ops JWT.
- `examples/hello-agent` — ~40-line reference agent showing the `agentkit` pattern.

**ultraQA** is built on this toolkit but ships as its **own repo**, extracted
from this repo (see [`docs/agent-standard.md`](docs/agent-standard.md) for the
extraction contract): the tool-using/ReAct agentic tester that drives an external
SUT end-to-end (browser via MCP + guarded http/db) behind a fail-closed egress
guard (ADR-0008, BR-011…016).

## Stack

Python 3.12 · FastAPI · **LangGraph-as-a-library** (`StateGraph` + Functional
API, embedded) · Postgres + pgvector · Redis · RabbitMQ ·
Ollama embeddings (`nomic-embed-text`, `vector(768)`) · OpenAI-compatible LLM
gateway · optional OpenTelemetry tracing · Docker / compose / Caddy · **uv
workspace**.

### Workspace layout (uv)

The root `pyproject.toml` is a virtual workspace aggregator
(`members = ["packages/*", "agents/*", "apps/*"]`). Each member is an
independently deployable package that depends on `agentkit` as a path
dependency (`agentkit = { workspace = true }`). `uv sync` installs all the
agents editable in one environment.

```
agent-bakery/
├── packages/agentkit       # the shared toolkit (keystone)
├── agents/monitoring       # agent/meta monitor (scheduled probes → alerts)
├── apps/dashboard          # HTMX ops console (HTTP fan-out)
├── examples/hello-agent    # ~40-line agent showing the pattern
├── infra/                  # bootstrap.sql, Caddy edge, socket-proxy
└── docs/                   # the SDD spec (read-first; see docs/README.md)
```

## Commands

Per-member tests and lint (each suite is self-contained):

```bash
uv sync                                  # install the whole workspace editable
uv run pytest packages/agentkit/tests    # toolkit
uv run pytest agents/monitoring/tests     # monitor
uv run pytest apps/dashboard/tests        # dashboard
uv run ruff check .                       # lint (broad ruleset) across the workspace
uv run ruff format .                      # format
uv run mypy packages agents apps          # types (strict — blocking in CI)
```

Tests are organized by **spec hierarchy**, not code layout: `tests/stories/`
(US-NNN), `tests/rules/` (BR-NNN), `tests/domain/` (entities). Every test header
carries `@spec US-NNN` / `@spec BR-NNN`.

## Read before Code (Spec-Driven Development)

This project follows SDD. **Spec ahead of code** — change behavior → change the
doc first, then the code. The spec lives in `docs/`.

**Foundation (always):**

- docs/README.md — the docs map (start here)
- docs/vision.md — why the whole project exists

**Product spec (what the system must do):**

- docs/user-stories.md — behavior as "As X I want Y so that Z" + Given/When/Then (US-NNN)
- docs/functional-decomposition.md — capability tree → US/BR leaves
- docs/domain-model.md — entities, relations, invariants, state machines
- docs/business-rules.md — cross-cutting rules + audit SQL (BR-NNN)

**Engineering spec (how it is built):**

- docs/architecture.md — layers, members, data flow, isolation
- docs/adr/decisions.md — the "library not paid Server" decision

**Member-level context (read when working in that member):**

- packages/agentkit/README.md — toolkit module map
- docs/deploy-your-own.md · docs/add-your-own-agent.md — operate / extend

## Iron rules (checklist — enforced by business-rules.md)

1. **Outbound / tool-using agents are fenced** (BR-011/012/013). An agent that
   acts against an external system runs only against an `ENV=dev` host:port
   allowlist (refuses to boot otherwise), routes every call through one egress
   guard (mutating verbs default-denied unless safe-write-allowlisted), and holds
   a non-admin disposable identity.
2. **Per-tenant isolation** (BR-002). Every agent store call is scoped by
   `tenant_id` from the `Principal`. `end-user`/`operator` are confined to one
   tenant; **only `ops`** may cross tenants (US-013), by design.
3. **Every LLM call goes through `agentkit.LLMClient`** with a per-request USD
   ceiling (BR-006) and a per-job cost cap (BR-007). No direct gateway calls.
4. **Alerts go through RabbitMQ `agent.alerts`** (agentkit `notify`), never a
   direct chat-platform API call from inside an agent. Dedup first: one firing
   Incident per `dedup_key`, alert once (BR-009).
5. **Secrets never live in the repo** (BR-010). Config from env via
   `BaseAgentSettings`; the repo ships only `env.example` (never `.env*`). No
   business identifiers, host IPs, or real tenant names. Per-agent tenants
   `acme`/`demo`; gateway `https://your-gateway.example.com/v1`.
6. **Library, not the paid Server** (ADR-0001). Embed OSS LangGraph
   (`StateGraph` / Functional API) in each agent's process. Do **not** adopt
   LangGraph Platform/Server, license keys, or `RemoteGraph`. Cross-agent calls
   are plain HTTP / AMQP.
7. **Per-agent isolation** (ADR-0001). One Postgres DB + one Redis index per
   agent; no cross-agent table access. agentkit and the dashboard own **no**
   persistent domain data.
8. **Spec ahead of code** (SDD). New behavior → a US in `user-stories.md` first;
   new rule → a BR; new entity/field → `domain-model.md`; architectural change →
   an ADR. Each spec item must keep a verification trace (test `@spec`, log
   event, DB/audit query).

## Engineering conventions

- **Typed contracts.** Pydantic models between every layer; structured LLM output
  validated, not trusted.
- **Errors are handled, never swallowed.** No bare `except: pass`. Surface or log
  with context (structlog).
- **No `print` in production code** — structlog JSON via agentkit `observability`.
- **Minimal change.** Touch only what the task requires; no drive-by refactors in
  an unrelated change.
- **Module reuse.** New agents subclass `BaseAgentSettings` and call
  `create_app()` — get health endpoints, auth, persistence, metrics for free
  (US-012). See docs/add-your-own-agent.md.

### Layer-specific rules

- [docs/conventions/llm.md](docs/conventions/llm.md) — LLM-agent patterns:
  cost ceiling, structured output via the gateway, prompt-injection guard.
- [docs/conventions/tests.md](docs/conventions/tests.md) — offline tests, fakes for
  LLM/db/RabbitMQ, keep every suite green, per-tenant isolation tests.

## Workflow for a non-trivial change

1. Read docs/architecture.md — find where the change lands (which member).
2. Architectural change → update `architecture.md` or write an ADR
   (`docs/adr/NNNN-*.md`, append-only) before code.
3. Product change → add/extend a US in `user-stories.md` (+ BR / domain-model if
   needed) before code.
4. Implement bottom-up per member: settings → graph nodes / repos → routes →
   tests under `tests/{stories,rules,domain}/`.
5. Add tests for the critical rule (BR) or story (US) you touched, with `@spec`.
6. Run that member's `pytest` + `ruff` + `mypy` green before opening a PR.

## Anti-patterns (do not)

- ❌ An outbound agent reaching prod or firing a mutating request outside the egress guard (BR-011/012)
- ❌ Any query path that crosses a tenant for a non-`ops` principal (BR-002)
- ❌ Direct LLM gateway calls bypassing `LLMClient` / the cost ceiling (BR-006)
- ❌ Direct chat-platform API calls from an agent (use RabbitMQ `notify`)
- ❌ Adopting LangGraph Server/Platform or committing a license key (ADR-0001)
- ❌ Cross-agent table access; agentkit/dashboard owning domain data (ADR-0001)
- ❌ Any business identifier, host IP, real tenant name, secret, or `.env` file
- ❌ `print` / swallowed exceptions / unvalidated LLM output
- ❌ Code without a spec item + verification trace

## Commit checklist

- [ ] Behavior change has a US / BR / entity entry (spec ahead of code)
- [ ] Touched only what the task required (no drive-by refactor)
- [ ] Outbound agents route through the egress guard; tenant isolation held
- [ ] LLM calls go through `LLMClient`; cost ceiling respected
- [ ] No secrets / business identifiers / `.env` files; only `env.example`
- [ ] New ADR if architecture changed (append-only, next NNNN)
- [ ] Member `pytest` + `ruff` + `mypy` green; tests carry `@spec`
