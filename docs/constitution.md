# Constitution — Iron Rules

**Date:** 2026-06-15
**Type:** Constraints. Breaking a rule requires a recorded reason in an ADR.

The iron rules of agent-bakery: keep the public OSS repo safe (zero secrets/identifiers), protect the AI-safety boundary (autonomous/tool-using agents are fenced), keep the architecture intact (LangGraph as a library, per-tenant isolation, agents as workspace members on a thin shared `agentkit`). Rules change **only through an ADR** ([adr/decisions.md](adr/decisions.md)). IDs cross-reference [user-stories.md](user-stories.md) (US-) and [business-rules.md](business-rules.md) (BR-).

---

## Code rules

- **CR-1:** Parameterized SQL only — bound parameters (`$1, $2, …`) via the `db` pool; never interpolate into SQL. Tenant scoping (`tenant_id`) is always bound from the verified `Principal`, never request input (BR-002).
- **CR-2:** Typed Pydantic contracts at every boundary (HTTP, LLM structured output, event payloads); no untyped `dict` crosses a layer. LLM structured output is schema-validated on the input boundary.
- **CR-3:** One change = one logical purpose; do not refactor untouched code. Public surfaces (agentkit exports, agent HTTP contracts) change additively; breaking changes need a version bump + migration note.
- **CR-4:** All output through agentkit `observability` (structlog JSON) — no `print()` shipped; secret-typed and PII fields redacted before any log line (BR-010).

---

## Architectural rules

- **AR-1:** Agents embed the OSS LangGraph library (`StateGraph` / Functional API) in their own process; never LangGraph Server/Platform/Studio, a license/LangSmith key, or any license-beacon runtime. Cross-agent comms are plain HTTP/AMQP, not `RemoteGraph` (ADR-0001; rejected in NS-003).
- **AR-2:** Each agent/app is an independently deployable uv workspace member sharing exactly one toolkit (`packages/agentkit`); agents never import each other's internals. The only shared seam is `agentkit` modules: `config · llm · observability · server · auth · db · notify`. Adding an agent must not modify agentkit or another agent's schema (US-012).
- **AR-3:** Each agent owns its Postgres database/schema and Redis logical index; no agent reads/writes another's tables. Cross-agent reads go over HTTP (dashboard fan-out, US-007/US-013) or AMQP, never into another agent's storage.
- **AR-4:** All config loads from env via a `BaseAgentSettings` subclass; imports define types/functions only — no module-level side effects (`client = connect()`). Pools/graphs/clients are built in the app/factory lifecycle and injected.
- **AR-5:** Alerts are published to the `agent.alerts` topic via agentkit `notify` and delivered by a separate service; an agent never calls a chat/messaging API directly (BR-009, US-011).
- **AR-6:** A behavioral/architectural change updates the spec first ([architecture.md](architecture.md), the US-/BR- entry, or an ADR), then the code. The spec is the source of truth.

---

## AI-safety rules

- **SAFE-1:** Any outbound / tool-using agent is fenced: it runs only against an allow-listed dev target (env-gated, fail-closed — refuses to boot otherwise), every external call traverses one egress guard, mutating verbs are default-denied unless explicitly safe-write-allowlisted, and it authenticates as a non-admin disposable identity (BR-011/BR-012/BR-013). The defining safety boundary, guarded by audit queries that must return zero rows.
- **SAFE-2:** Where the choice exists, prefer read-only or human-gated action over acting autonomously; new agent capabilities default to review-gated, not auto-applied.
- **SAFE-3:** Every LLM call goes through agentkit `LLMClient`, which meters USD cost and enforces a per-request ceiling (BR-006); multi-step jobs also respect a run-level cost cap (BR-007). A call/job exceeding its bound is aborted/truncated, never silently overspent.

---

## Security rules

- **SR-1:** Public repo — no real secret, API key, JWT secret, host IP, or credential is ever committed; ship `env.example` placeholders only, loaded by `BaseAgentSettings`. Never read/write/create `.env*` files. Secret-scanning runs in CI (BR-010).
- **SR-2:** Zero business identifiers in the repo — no private brand, product, host, tenant name, host IP, or real keys. Examples use tenants `acme` / `demo`; the LLM gateway is `https://your-gateway.example.com/v1` via env (`LLM_BASE_URL`) (BR-010).
- **SR-3:** Every agent read/write is scoped by `tenant_id` from the verified `Principal`; a query without a tenant filter is a security incident. Roles `end-user`/`operator` are confined to one tenant; the only boundary-crossing identity is role `ops` (the dashboard's minted JWT), cross-tenant by design (BR-002, US-013).
- **SR-4:** Agents verify HS256 JWTs into a `Principal` (agentkit `auth`) and run no end-user login (NS-002). Secret values are never serialized into a response or log line.
- **SR-5:** Agent content, JWTs, and secrets are never logged at full fidelity; long free-text is redacted/metadata-only by default. Tracing (OpenTelemetry) is opt-in, off by default; raw content in traces is a deliberate debug-only opt-in.

---

## Workflow rules

- **WR-1:** `uv run pytest` shall pass for every touched member before a change is done; a change that turns a green suite red is not done. Each US-/BR- entry names its verifying test; new behavior ships with the test that proves it.
- **WR-2:** Every non-trivial change shall begin by naming what changes and which problem it solves ("Changing X because Y").
- **WR-3:** Any architectural choice (new datastore, deployment-model change, breaking contract, new external dependency) shall get an ADR in `docs/adr/` with consistent cross-reference IDs (US-NNN / BR-NNN / ADR-NNNN).
- **WR-4:** One commit shall equal one logical change — not "fix typo + add feature + refactor".
- **WR-5:** On a failure, form 2–3 hypotheses, weigh them, criticize the obvious pick, then try the simplest first — not first-guess → code.

---

## Anti-patterns (never allowed)

SQL string interpolation · a query/mutation without a tenant filter (except the `ops` path) · an outbound agent reaching prod or firing a mutating/destructive request outside the egress guard · adopting LangGraph Server/Platform/Studio or any license-keyed runtime · one agent importing another's internals or reading its tables · any secret/key/host IP/private brand or tenant name in the repo · reading/writing/creating `.env*` files (only `env.example` is tracked) · `print()` or unstructured logging in shipped code · module-level side effects (`client = connect()` at import) · direct chat-platform calls from an agent (publish to `agent.alerts`) · an uncosted LLM call bypassing `LLMClient` · refactoring unrelated code inside a focused change · "temporary" hacks without a TODO + tracked issue.

---

## Process to change these rules

Open an ADR ([adr/decisions.md](adr/decisions.md)) `proposed` (what/why/alternative/migration) → implement on a branch → works: ADR `accepted`, update the rule here; doesn't: ADR `superseded`/`rejected`, the rule stands.

---

## Related

- [principles.md](principles.md) — engineering principles (softer than these)
- [architecture.md](architecture.md) — modules, layers, data flow
- [adr/decisions.md](adr/decisions.md) — library, not platform
- [business-rules.md](business-rules.md) · [user-stories.md](user-stories.md) — BR-/US- contracts
