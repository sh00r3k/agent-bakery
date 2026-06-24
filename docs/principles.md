# Engineering Principles

**Date:** 2026-06-15

Softer than [constitution.md](constitution.md), but load-bearing. Breaking one is a **signal** something is off, not automatically a violation. Iron rules say _what you must not do_; these say _how we prefer to build_ so the iron rules stay easy to keep.

---

## 1. Thin shared toolkit

`agentkit` owns only cross-cutting seams (`config · llm · observability · server · auth · db · notify`), nothing agent-specific; business logic stays in the agent that owns it. A thin seam keeps agents independent and swappable (US-012, AR-2). Anti-pattern: agent-specific helpers in `agentkit` before a second agent needs them.

## 2. Env-driven config, twelve-factor

Config flows from the environment into a `BaseAgentSettings` subclass; one image runs dev/CI/prod, only env differs. No hardcoded URLs/ports/secrets — LLM gateway is `LLM_BASE_URL` (`https://your-gateway.example.com/v1`), embeddings default to local Ollama. Reproducible deploys, no leakage (SR-1/BR-010).

```python
class MyAgentSettings(BaseAgentSettings):
    agent_name: str = "my-agent"
    run_cost_cap_usd: float = 1.00   # tunable per-env, never hardcoded inline
```

## 3. Observability built-in, not bolted on

Every agent emits structured JSON logs and `/metrics.json` from day one (agentkit `server` + `observability`). Named domain events (`signal.emitted`, `incident.alerted`, `finding.upserted`, `llm.call`) are the contract the monitoring agent and verification suite read (US-007, US-011). Tracing (OpenTelemetry) is optional, off by default.

## 4. Graceful degradation

A down dependency degrades a feature, never crashes the agents. `/readyz` (deps reachable) is distinct from `/healthz` (process alive). The dashboard renders panels for present agents, skips absent ones (US-013). Failures surface as a Signal/Incident, not swallowed.

## 5. Cost-aware LLM

LLM spend is a first-class budget. Every call goes through `LLMClient`, which meters USD and enforces a per-request ceiling (BR-006); multi-step jobs respect a run-level cap (BR-007). Pick the cheapest model meeting the bar, keep prompts tight, record `cost_usd`.

| Operation                       | Bound                                              |
| ------------------------------- | -------------------------------------------------- |
| Single LLM call (classify step) | per-request USD ceiling (`llm_max_cost_usd`)       |
| Multi-step job (e.g. a sweep)   | run-level cap, truncate on hit (BR-007)            |
| Embeddings                      | local Ollama, effectively free                     |

## 6. Safe autonomy

Prefer read-only or human-gated action over acting alone. Any outbound / tool-using agent (e.g. ultraQA) is env-gated and routes every external call through a fail-closed egress guard; destructive actions need a separately-gated human step (SAFE-1, BR-011/BR-012).

## 7. One task, one node / one function

Decompose. A LangGraph graph is small typed nodes (`explore → assert → record` for ultraQA), each doing one thing, independently testable. The monitoring pipeline likewise: `collect → evaluate → classify → dedup → notify`. Monolithic steps hide cost and defeat tracing.

## 8. Tenant scoping is structural, not incidental

Tenant scope (`tenant_id`) is bound from the verified `Principal` at the repository layer — never from request input, never optional. Cross-tenant access is a single explicit audited path (`ops`, US-013). Make the scoped query the only query.

## 9. Verify, don't issue, identity

The agents verify tokens, they don't run logins. Tenants own identity; agents verify an HS256 JWT into a `Principal` (agentkit `auth`); the dashboard mints short-lived `ops` JWTs for fan-out. An identity store we don't need is attack surface we don't want (NS-002).

## 10. Idempotent alerting

Anything retriable must tolerate replay. Alerting folds repeated Signals into one firing Incident per `dedup_key`, alerting once per firing transition (BR-009); ultraQA upserts a Finding by `dedup_key` rather than forking (BR-015).

## 11. Errors are reported, never swallowed

Catch to handle, classify, and surface — not to hide. An empty `except` is a bug. Failures become a structured log event, a degraded `/readyz`, or a Signal/Incident — with enough context (operation, ids) to act on, no leaked secrets/PII.

## 12. Spec-first, additive change

Non-trivial work: update the spec (architecture / US- / BR- / ADR) → implement by layer (schema → repository → service → HTTP/graph → UI) → test → reconcile docs. Additive where possible; breaking a contract needs a version bump and migration note (CR-3, AR-6).

---

## 13. Quick checklist before a PR

- [ ] Changing only what the task asked (no drive-by refactor)
- [ ] Stated "Changing X because Y"
- [ ] SQL is parameterized; tenant scope bound from `Principal`, not input
- [ ] LLM calls go through `LLMClient` (cost-bounded); outbound agents route through the egress guard
- [ ] No secrets / host IPs / private brand names; `env.example` only
- [ ] Structured logging (no `print`); secret/PII fields redacted
- [ ] Tests green for every touched agent; new behavior has a test (with `@spec US-/BR-`)
- [ ] Spec updated (architecture / US- / BR-) and an ADR added if architectural

---

## Sources

- [constitution.md](constitution.md) — iron rules (harder than these)
- [architecture.md](architecture.md) · [adr/decisions.md](adr/decisions.md)
- [ADR-0001](adr/decisions.md) — LangGraph as a library, self-hosted infra
- [The Twelve-Factor App](https://12factor.net/) — config/observability/disposability model
- [Anthropic — Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents) — workflow-over-agent, decomposition
