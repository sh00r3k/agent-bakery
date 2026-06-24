# Roadmap — agent-bakery

**Date:** 2026-06-15

Self-hosted OSS LangGraph agents on one shared infra layer + one Python toolkit
([architecture.md](architecture.md), [ADR-0001](adr/decisions.md)). Phases are
cumulative; each closes when its acceptance criteria pass. IDs reference
[user-stories.md](user-stories.md) (US-NNN), [business-rules.md](business-rules.md) (BR-NNN).

---

## Current status

**P0–P2 complete · P3 planned · Last update: 2026-06-15**

| Phase | Theme                                            | Status         |
| ----- | ------------------------------------------------ | -------------- |
| P0    | agentkit toolkit MVP                             | ✅ done        |
| P1    | Monitoring + dashboard                           | ✅ done        |
| P2    | Additional interfaces (CLI)                      | ✅ done        |
| P3    | ultraQA (agentic QA)                             | 📋 planned     |
| P4    | CI + public release                              | 🚧 in progress |

---

## P0 — agentkit toolkit MVP ✅ done

The shared contract; a new agent boots on it with health, metrics, auth, persistence,
and a cost-metered LLM for free.

- agentkit: `config` (A.1.1), `llm` chat + USD ceiling (A.2.1, A.2.3, BR-006),
  `server` + `/healthz` `/readyz` `/metrics.json` (A.4.1), `auth` JWT → Principal
  (A.5.1, BR-002), `db` (A.6).
- New-agent boot (US-012), tenant isolation (BR-002), secret hygiene (BR-010).

**Acceptance ✅** — `create_app` serves the three probes; LLM rejected over the USD
ceiling (BR-006); `acme`/`demo` isolated (BR-002); no repo secrets, `env.example`
everywhere (BR-010).

---

## P1 — Monitoring + dashboard ✅ done

See all agents and act on incidents from one console.

- monitoring: scrape the three probes (C.1.1), Docker state via read-only
  socket-proxy (C.1.2), host vitals + RabbitMQ depth (C.1.3).
- SLO rules → Signal: agent-down, error-spike, batch-overdue (C.2.1, **BR-008**).
- Triage → dedup → alert once per `dedup_key` (US-011, **BR-009**), publish to
  RabbitMQ `agent.alerts` (A.7.1).
- dashboard: config-driven registry, any composition (US-013); HTTP fan-out with a
  minted ops JWT (D.1.2, BR-002); panels health / incidents (US-007).

**Acceptance ✅** — stopping an agent raises exactly one `agent-down` Incident + one
alert (BR-008, BR-009), no re-alert while down; dashboard boots with one and three
agents, no code change (US-013).

---

## P2 — Additional interfaces ✅ done

Drive and ship the platform the same way across surfaces.

- **Private Mode** — `PRIVATE_MODE=true` blocks outbound TCP except an allow-list,
  skips optional tracing ([design-private-mode.md](design-private-mode.md)).
- **Root-compose distribution** + **operator `platform` CLI** over the registry ([ADR-0009](adr/decisions.md), [ADR-0011](adr/decisions.md)).

**Acceptance ✅** — `PRIVATE_MODE` blocks non-allow-listed egress (tested);
`platform up/down/agent/token/doctor` round-trips the `DASHBOARD_AGENTS` registry.

---

## P3 — ultraQA (agentic QA) 📋

The first tool-using / ReAct agent — drives a real product end-to-end against a dev
SUT to find defects, fenced by a fail-closed egress guard ([ADR-0008](adr/decisions.md)).

- Tool-calling on the shared seam: `LLMClient.complete_with_tools` (US-020, BR-006).
- Explore + assert against a spec oracle; Findings + CoverageNodes (US-014, US-015, US-018).
- Fail-closed envelope: env=dev only, guarded egress, non-admin creds (US-016, **BR-011/012/013**).
- Findings deduped + alerted; dashboard `features:["findings"]` panel (US-017, **BR-015/016**).

**Acceptance** — `ENV!=dev` or an unmatched target refuses to boot; every mutating
SUT verb is denied unless safe-write-allowlisted; a repeat defect upserts (no fork);
a critical Finding raises exactly one alert.

---

## P4 — CI + public release 🚧 in progress

A clean public OSS drop.

- Optional OpenTelemetry tracing (`OTEL_EXPORTER_OTLP_ENDPOINT`), off by default.
- CI: lint + typecheck + tests across the uv workspace ([ADR-0004](adr/decisions.md)).
- Release hygiene: scrub identifiers, generic tenants (`acme`/`demo`), gateway
  `https://your-gateway.example.com/v1` from env, `env.example` only.

**Acceptance** — agents run correctly with tracing off (the default); CI green;
repo scan finds zero business identifiers, host IPs, keys, or `.env` files.

---

## Future (post-P4 backlog)

> Appendix — not scheduled. Promote into a phase when picked up.

- More agents on `agentkit` (PM-bot, release-notes, billing-watcher).
- Auth hardening: key rotation, asymmetric JWT (RS256), short-lived ops tokens.
- Per-agent autoscaling / multi-host deployment.

---

## Out of scope (non-goals)

- ❌ Adopting LangGraph Platform/Server (paid runtime) — see ADR-0001, NS-003.
- ❌ Our own end-user identity/login database — host owns identity (NS-002).
- ❌ Direct chat-platform API calls from inside an agent — alerts go via RabbitMQ (A.7.1).

---

## Document version

- **v1.0** (2026-06-15) — initial roadmap.

When a phase closes or scope pivots: bump version, update the status table, and
record any architectural call in `adr/`.
