# Domain Model — agent-bakery

**Date:** 2026-06-15
**Type:** Business entities, fields, relations, invariants, state machines.

---

## What this is

The logical model of what the agents _mean_ — not the DB schema (per agent, see
[architecture.md](architecture.md)) and not the HTTP contract. Where a physical
table denormalizes or an API hides a field, that divergence is noted; this is the
source of truth for entity life-cycles and invariants.

Two domains own data:

| Bounded context      | Owner                                 | Key entities                                    |
| -------------------- | ------------------------------------- | ----------------------------------------------- |
| **Agent monitoring** | `agents/monitoring` (own Postgres DB) | Signal, Incident                                |
| **QA (ultraQA)**     | `agents/ultraqa` (own Postgres DB)    | QaRun, QaStep, Finding, CoverageNode            |

`packages/agentkit` and `apps/dashboard` own **no** persistent domain data. Per-agent
DB isolation is a hard invariant ([ADR-0001](adr/decisions.md), BR-002).

---

## Cross-cutting invariant: per-tenant isolation

Where an agent is multi-tenant, every store operation is scoped by `tenant_id` bound from
the verified `Principal` — never from request input. No query path crosses a tenant
boundary **except** an authenticated `ops` principal on the dashboard, which may read
across agents on purpose (BR-002). A normal `end-user`/`operator` JWT carries exactly one
`tenant_id`.

---

# Agent-monitoring entities

## Entity: `Signal`

A single raw observation an SLO rule fired on — e.g. an agent's `/readyz` failing, an
error-rate spike, an overdue batch. Emitted by a collector tick; classified; deduplicated
into an Incident; retained for history.

| Field         | Type        | Req | Note                                                                              |
| ------------- | ----------- | --- | -------------------------------------------------------------------------------- |
| `id`          | UUID        | yes | Primary key                                                                       |
| `rule`        | enum        | yes | `agent-down` \| `error-spike` \| `batch-overdue`                                  |
| `target`      | TEXT        | yes | What it concerns (agent name / host / queue)                                     |
| `severity`    | enum        | yes | `info` \| `warning` \| `critical`                                                |
| `dedup_key`   | TEXT        | yes | Stable key `(rule, target)` used to collapse repeats                             |
| `detail`      | JSONB       | yes | Evidence (metric values, thresholds)                                            |
| `observed_at` | timestamptz | yes |                                                                                  |

**Relations:** rolls up into Incident (N:1 via `dedup_key`).

**Invariants:** `dedup_key` deterministic from `(rule, target)` so repeats collapse; a
Signal never alerts on its own — must pass through dedup into an Incident.

**Verification:** DB `signals`; tests `test_signal.py`; event `signal.emitted` (rule,
target, severity, dedup_key).

---

## Entity: `Incident`

A deduplicated, alertable problem aggregating one or more Signals with the same `dedup_key`.
The unit operators are notified about.

| Field           | Type        | Req | Note                                            |
| --------------- | ----------- | --- | ----------------------------------------------- |
| `id`            | UUID        | yes | Primary key                                     |
| `dedup_key`     | TEXT        | yes | Unique while firing — one open Incident per key |
| `rule`          | enum        | yes | Same vocabulary as `Signal.rule`                |
| `target`        | TEXT        | yes |                                                 |
| `severity`      | enum        | yes | Highest severity seen across its Signals        |
| `status`        | enum        | yes | `firing` \| `resolved`                          |
| `signal_count`  | int         | yes | Number of Signals folded in                     |
| `first_seen_at` | timestamptz | yes |                                                 |
| `last_seen_at`  | timestamptz | yes |                                                 |
| `resolved_at`   | timestamptz | no  |                                                 |
| `notified_at`   | timestamptz | no  | When an alert was published to RabbitMQ         |

**Relations:** has many Signal (1:N via `dedup_key`).

**Invariants:**

- At most one Incident with a given `dedup_key` has `status = 'firing'` (BR-009).
- An alert is published at most once per firing transition (dedup prevents alert storms).
- `status = 'resolved'` ⇒ `resolved_at IS NOT NULL`.

**Verification:** DB `incidents` (partial unique index on `dedup_key WHERE status='firing'`);
tests `test_incident_state_machine.py`; events `incident.opened`, `incident.alerted`,
`incident.resolved`.

```sql
-- at most one firing incident per dedup_key
SELECT dedup_key, count(*) FROM incidents WHERE status='firing'
GROUP BY 1 HAVING count(*) > 1;
-- resolved incidents must carry resolved_at
SELECT id FROM incidents WHERE status='resolved' AND resolved_at IS NULL;
```

---

# QA (ultraQA) entities

## Entity: `QaRun` (ultraQA)

One ultraQA sweep execution against the SUT. Owns its steps and findings. Source:
[ADR-0008](adr/decisions.md).

| Field          | Type        | Req | Note                                             |
| -------------- | ----------- | --- | ------------------------------------------------ |
| `id`           | UUID        | yes | Primary key                                      |
| `sut_env`      | TEXT        | yes | Always `dev` — invariant (BR-011)                |
| `trigger`      | enum        | yes | `scheduled` \| `manual`                          |
| `status`       | enum        | yes | `running` \| `done` \| `aborted`                 |
| `steps_count`  | int         | yes | ReAct steps taken                                |
| `llm_cost_usd` | numeric     | yes | Cumulative metered cost (BR-006/007, BR-014)     |
| `coverage_pct` | numeric     | no  | Explored / (explored+unexplored) at end (US-018) |
| `started_at`   | timestamptz | yes |                                                  |
| `finished_at`  | timestamptz | no  |                                                  |

**Relations:** has many QaStep (1:N); produces many Finding (1:N via run_id).

**Invariants:**

- `sut_env = 'dev'` always (BR-011); any other value is a violation.
- `llm_cost_usd <= max_cost_usd` for the run (BR-014); breach ⇒ `status='aborted'`.
- `status='done'|'aborted'` ⇒ `finished_at IS NOT NULL`.

**Verification:** DB `qa_runs`; audit `SELECT id FROM qa_runs WHERE sut_env<>'dev';`
(0 rows); tests `test_qa_run.py`.

---

## Entity: `QaStep` (ultraQA)

One action in a run — a tool call (browser/http/db) and its egress decision. Records the
guard verdict that proves BR-012.

| Field            | Type        | Req | Note                                                                   |
| ---------------- | ----------- | --- | --------------------------------------------------------------------- |
| `id`             | UUID        | yes | Primary key                                                            |
| `run_id`         | UUID        | yes | FK → `qa_runs`                                                         |
| `tool`           | TEXT        | yes | `browser` \| `http_probe` \| `db_readonly` \| `spec_lookup` \| …       |
| `verb`           | TEXT        | no  | HTTP method when applicable                                            |
| `target`         | TEXT        | no  | Host/route touched                                                     |
| `safe_write`     | bool        | yes | True only if on the reviewed safe-write allowlist                      |
| `forwarded`      | bool        | yes | Whether the guard let it reach the SUT                                 |
| `blocked_reason` | TEXT        | no  | Set when `forwarded=false` (e.g. `denylist`, `verb-deny`, `host-deny`) |
| `created_at`     | timestamptz | yes |                                                                       |

**Invariants:** a mutating verb (`verb <> 'GET'`) with `safe_write=false` MUST have
`forwarded=false` (BR-012). Audit: `SELECT id FROM qa_steps WHERE forwarded AND verb<>'GET'
AND NOT safe_write;` (0 rows).

**Verification:** DB `qa_steps`; tests `test_br012_guard.py`.

---

## Entity: `Finding` (ultraQA)

A deduplicated defect ultraQA observed — a crash, an error response, a broken state, or a
divergence from the spec oracle. The unit the dashboard renders and (when severe) alerts on.
Repeat observations bump `count`/`last_seen` (BR-015); they do not fork.

| Field         | Type        | Req | Note                                                                          |
| ------------- | ----------- | --- | ---------------------------------------------------------------------------- |
| `id`          | UUID        | yes | Primary key                                                                   |
| `dedup_key`   | TEXT        | yes | `sha256(check\x00target\x00signature)[:32]`, UNIQUE (BR-015)                  |
| `severity`    | enum        | yes | `info` \| `warning` \| `critical` only (BR-016)                               |
| `check`       | TEXT        | yes | What was verified (e.g. `console-error`, `http-5xx`, `spec-divergence`)       |
| `target`      | TEXT        | yes | Where (route/page/flow)                                                       |
| `title`       | TEXT        | yes | One-line human summary                                                        |
| `signature`   | TEXT        | yes | Normalized symptom / spec-ref used in `dedup_key`                             |
| `spec_ref`    | TEXT        | no  | The oracle US/BR the divergence violates (US-015)                             |
| `repro_steps` | jsonb       | no  | Ordered steps to reproduce                                                    |
| `remediation` | TEXT        | no  | Suggested fix, if any                                                         |
| `status`      | enum        | yes | `open` \| `confirmed` \| `fixed` \| `dismissed`                               |
| `count`       | int         | yes | Times observed                                                                |
| `first_seen`  | timestamptz | yes |                                                                              |
| `last_seen`   | timestamptz | yes |                                                                              |

**Relations:** belongs to QaRun (first/last producing run).

**Invariants:**

- `dedup_key` UNIQUE; a second observation upserts (BR-015). Audit:
  `SELECT dedup_key FROM findings GROUP BY 1 HAVING count(*)>1;` (0 rows).
- `severity IN ('info','warning','critical')` (BR-016). Audit:
  `SELECT id FROM findings WHERE severity NOT IN ('info','warning','critical');` (0 rows).
- `status='fixed'` ⇒ a sweep after `last_seen` failed to reproduce.

**Verification:** DB `findings` (`UNIQUE(dedup_key)`, `CHECK(severity …)`); served by
`GET /findings`; consumed by dashboard `features:["findings"]`; tests
`agents/ultraqa/tests/{rules/test_br015_finding_dedup,rules/test_br016_severity_enum,stories/test_us017_findings}.py`.

---

## Entity: `CoverageNode` (ultraQA)

A unit of the SUT surface (a route or UI page) and how far ultraQA has exercised it — the
map that makes exploration coverage-driven (US-018).

| Field           | Type        | Req | Note                                                     |
| --------------- | ----------- | --- | -------------------------------------------------------- |
| `id`            | UUID        | yes | Primary key                                              |
| `kind`          | enum        | yes | `route` \| `page`                                        |
| `path`          | TEXT        | yes | Route path or page URL; UNIQUE with `kind`               |
| `risk_tier`     | enum        | yes | `safe` \| `destructive` (from the BR-012 classification) |
| `status`        | enum        | yes | `unexplored` \| `explored` \| `blocked`                  |
| `last_explored` | timestamptz | no  |                                                          |

**Invariants:** a `destructive` node is `blocked`, never `explored`, in autonomous mode
(ADR-0008 §(c)); `coverage_pct` (US-018) counts only `safe` nodes: explored /
(explored+unexplored).

**Verification:** DB `coverage_nodes` (`UNIQUE(kind,path)`); tests `test_us018_coverage.py`.

---

## State Machines

### `Incident.status`

```
[firing] ──condition clears / quiet window──> [resolved]
   ▲                                              │
   └──────────────── recurs ──────────────────────┘
```

| From       | To         | Triggered by                        | Conditions                |
| ---------- | ---------- | ----------------------------------- | ------------------------- |
| (none)     | `firing`   | first Signal for a new `dedup_key`  | no open Incident for key  |
| `firing`   | `resolved` | clear signal / quiet window elapsed |                           |
| `resolved` | `firing`   | matching Signal recurs              | reuses or reopens the key |

### `Finding.status` (ultraQA)

```
[open] ──re-observed/verified──> [confirmed] ──no longer reproduces──> [fixed]
   │                                  │                                   ▲
   └────────── triaged ───────┐      └──────── triaged ──────┐           │
                              ▼                              ▼           │
                         [dismissed]                    [dismissed]   recurs → [open]
```

| From               | To          | Triggered by                   | Conditions                                    |
| ------------------ | ----------- | ------------------------------ | --------------------------------------------- |
| (none)             | `open`      | first observation of a defect  | new `dedup_key`                               |
| `open`             | `confirmed` | re-observed or verified        | same `dedup_key` seen again (bumps `count`)   |
| `open`/`confirmed` | `fixed`     | a later sweep cannot reproduce | reproduction attempt failed after `last_seen` |
| `open`/`confirmed` | `dismissed` | operator triage (not-a-bug)    | —                                             |
| `fixed`            | `open`      | regression — defect recurs     | re-observed after `fixed`                     |

**Forbidden:** creating a second row for an existing `dedup_key` (must upsert, BR-015); a
`severity` outside `{info,warning,critical}` (BR-016).

---

## Aggregates (DDD-light)

| Aggregate Root | Includes                                | Boundary                                                          |
| -------------- | --------------------------------------- | ----------------------------------------------------------------- |
| `Incident`     | `Incident` + folded `Signal`s           | Dedup/fold happens inside the incident aggregate                  |
| `QaRun`        | `QaRun` + its `QaStep`s + its `Finding`s | One sweep mutates inside the run; Findings upsert by `dedup_key`  |

Mutate only through the root; one transaction = one aggregate; cross-aggregate references
by id only.

---

## Value Objects (immutable)

| Value Object                   | Description                  | Example                              |
| ------------------------------ | ---------------------------- | ------------------------------------ |
| `CostUSD(amount)`              | LLM spend, USD               | `CostUSD(0.0021)`                    |
| `DedupKey(rule, target)`       | Stable monitoring collapse key | `agent-down:monitoring`            |
| `Principal(sub, tenant, role)` | Decoded JWT identity (agentkit auth) | `Principal("u-12","acme","ops")` |

---

## Domain events

Append-only, emitted as structlog JSON and (for alerts) over RabbitMQ `agent.alerts`.

- `signal.emitted`, `incident.opened`, `incident.alerted`, `incident.resolved` — monitoring
- `llm.call` (cost_usd) — any agent's LLM seam
- `run.started`, `run.finished`, `run.cost_cap_hit`, `finding.upserted` — ultraQA

`incident.alerted` (and a critical `finding.upserted`) are the only events that egress to
operators (via the notification microservice consuming `agent.alerts`); everything else
stays in logs/DB.

---

## Domain → DB / API divergence

| Domain entity   | DB table                | Notes                                        |
| --------------- | ----------------------- | -------------------------------------------- |
| Signal/Incident | `signals` / `incidents` | live only in the monitoring DB               |
| QaRun/QaStep    | `qa_runs` / `qa_steps`  | live only in the ultraQA DB                  |
| Finding         | `findings`              | `UNIQUE(dedup_key)`; served by `GET /findings` |
| CoverageNode    | `coverage_nodes`        | `embedding` (SUT oracle) is internal         |

---

## Related docs

- [user-stories.md](user-stories.md) — behavior over these entities (US-NNN)
- [business-rules.md](business-rules.md) — invariants as enforced rules (BR-NNN)
- [functional-decomposition.md](functional-decomposition.md) — capabilities operating on them
- [architecture.md](architecture.md) — where entities physically live
