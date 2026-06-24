# Business Rules — agent-bakery

**Date:** 2026-06-15
**Type:** Cross-cutting business rules, policies, and invariants. Source of truth for "why is it built this way".

---

## What this is

Business/product rules that shape system behavior, distinct from engineering conventions. Each rule is enforced and tested somewhere; where it touches persisted state it is auditable with a SQL query that must return zero rows. IDs (BR-NNN) are referenced by [domain-model.md](domain-model.md) and [user-stories.md](user-stories.md).

Each rule below is a tight block: a testable statement, its source ref, and its audit query (if any).

Categories: Tenancy & isolation · Cost · Alerting · Secrets · ultraQA · Edge cases · Versioning.

---

## Tenancy & isolation rules

**BR-002:** Every agent store call shall be scoped by `tenant_id` derived from the verified `Principal`, never from request input. A `Principal` with role `end-user` or `operator` is confined to its single `tenant_id`; the only boundary-crossing identity is role `ops` (the dashboard's minted JWT), which may read across tenants by design.

| Principal role | Cross-tenant? | Condition                                      |
| -------------- | ------------- | ---------------------------------------------- |
| `end-user`     | No            | confined to its tenant                         |
| `operator`     | No            | confined to its tenant                         |
| `ops`          | Yes           | minted by dashboard; reads all agents (US-013) |

Source: [ADR-0001](adr/decisions.md) (per-agent isolation) + dashboard ops-console requirement. Impl: every SQL builder binds `tenant_id` from the `Principal`; the dashboard mints a short-lived `ops` JWT per fan-out call. Test: agentkit per-tenant scoping tests (an `operator` crossing tenants → 403; `ops` → 200).
Related: US-013; BR-006.

---

## Cost rules

**BR-006:** Every LLM call shall go through agentkit's `LLMClient`, which meters `cost = tokens_in*price_in + tokens_out*price_out`. If `cost` would exceed `settings.llm_max_cost_usd`, then raise `CostCeilingExceeded`; otherwise record cost on the operation.
Source: agentkit design; [packages/agentkit/README.md](../packages/agentkit/README.md). Impl: `packages/agentkit/.../llm.py`. Test: `packages/agentkit/tests/test_br006_cost_ceiling.py`. Log: `llm.call` with `cost_usd`; `llm.cost_ceiling_exceeded` on abort.
Related: US-020, BR-007.

**BR-007:** A multi-step LLM job (e.g. an ultraQA sweep) shall track cumulative cost; if it reaches the job's configured cost cap, then the run stops and persists partial cost rather than overrunning. Each step is also bounded by BR-006.

```
running_cost = 0
for each LLM step: running_cost += step.cost (each step also bounded by BR-006)
  if running_cost >= job.cost_cap_usd: stop, persist what exists
```

Source: agentkit cost meter at the job level. Test: `agents/ultraqa/tests/rules/test_br014_loop_bounded.py`. Log: `run.cost_cap_hit`. Audit: see BR-006.
Related: US-014, BR-006, BR-014.

---

## Alerting rules

**BR-008:** The monitoring agent shall fire Signals from a fixed rule set evaluated each tick: `agent-down` (failing `/healthz`/`/readyz`), `error-spike` (error rate over threshold from `/metrics.json`), `batch-overdue` (a scheduled job missed its window). Each Signal carries a deterministic `dedup_key = (rule, target)` and a severity. Trigger: collector tick (scrapes agents, Docker socket-proxy, host vitals, RabbitMQ depth).
Source: Monitoring agent spec. Test: `agents/monitoring/tests/rules/test_br008_slo_rules.py` (one fixture per rule). Log: `signal.emitted` (rule, target, severity, dedup_key).
Related: US-011.

**BR-009:** Signals shall fold into Incidents by `dedup_key`: at most one Incident per key is `firing` at a time, and an alert is published to RabbitMQ `agent.alerts` at most once per firing transition. Recurring signals update `last_seen_at`/`signal_count` and raise severity if higher without re-alerting; clearing resolves the Incident.

```
on Signal s:
  inc = open incident where dedup_key = s.dedup_key
  if none: create firing Incident; publish ONE alert to agent.alerts; set notified_at
  else: increment signal_count, bump last_seen_at, raise severity if higher  (no new alert)
```

Source: Monitoring agent spec. Test: `agents/monitoring/tests/rules/test_br009_dedup.py` (N signals → 1 incident → 1 alert). Log: `incident.opened`, `incident.alerted` (once), `incident.resolved`.
Audit (must be 0):

```sql
SELECT dedup_key, count(*) FROM incidents WHERE status='firing'
GROUP BY 1 HAVING count(*) > 1;  -- must be 0
```

Related: US-011.

---

## Secrets rules

**BR-010:** No real secret, key, host IP, or business/tenant identifier shall be committed to this public repo. Secrets load from env (`BaseAgentSettings`); the repo ships only `env.example` placeholders. Docs use generic tenants (`acme`, `demo`) and reference the gateway as `https://your-gateway.example.com/v1` via env. Secret values are never serialized into a response or log line.
Source: Public-repo policy. Impl: `BaseAgentSettings` env loading; response models exclude secrets; log redaction in agentkit `observability`. Test: `packages/agentkit/tests/test_br010_no_secret_leak.py` + CI secret-scan (e.g. gitleaks). Audit: CI grep for forbidden identifiers / private IPs in tracked files (must be empty).
Related: BR-006 (gateway URL via env).

**BR-017:** Secret rotation shall occur on a quarterly schedule OR immediately upon personnel change OR after any suspected/confirmed incident. All long-lived secrets in the system:

| Secret               | Rotation method                                                                  | Impact of rotation                                    |
| -------------------- | -------------------------------------------------------------------------------- | ----------------------------------------------------- |
| `JWT_SECRET`         | Replace env var on all agents; old tokens expire at their `exp` (default 15 min) | All active sessions invalidated; clients must re-auth |
| `LLM_API_KEY`        | Replace env var; LLMClient picks up on next request                              | No disruption (gateway-side key rotation is instant)  |
| `REDIS_PASSWORD`     | Replace env var + restart Redis container                                        | Brief connection blip during restart                  |
| `RABBITMQ_USER/PASS` | Replace env var + restart RabbitMQ container                                     | Brief connection blip during restart                  |
| `POSTGRES_PASSWORD`  | Replace env var + `ALTER USER` in Postgres + restart agent containers            | Brief connection blip                                 |

Procedure: (1) Generate new secret via `openssl rand -hex 32`; (2) Update `.env` on host; (3) Restart affected containers; (4) Verify `/healthz` + `/readyz` on each agent; (5) Invalidate old credentials at the source (gateway UI, DB `ALTER USER`, etc.).

Source: BR-010 (no secrets in repo), operational security best practice. Test: rotation procedure is validated in staging quarterly.

---

## QA agent (ultraQA) rules

These govern the first tool-using/outbound agent. Source: **ADR-0008**. "SUT" = the external system-under-test (the target dev environment).

**BR-011:** ultraQA shall act against the dev контур only, fail-closed. When `ENV != "dev"` OR the resolved SUT target is not in the host:port allowlist, then the agent refuses to start (RuntimeError) and runs no sweep; with an empty/unset allowlist, zero bytes are sent to any SUT.
Source: ADR-0008 §(c); AR-4 (env-driven), SAFE-2 (guard, don't trust). Impl: `ultraqa.settings` (`env`, `sut_allowlist`); `ultraqa.guard` start check. Test: `agents/ultraqa/tests/rules/test_br011_dev_only.py` (`@spec BR-011`) — start refused for `ENV=prod`; navigation to a non-allowlisted host refused.
Audit (must return 0 rows):

```sql
SELECT id FROM qa_runs WHERE sut_env <> 'dev';
```

Related: BR-012, BR-013, US-016.

**BR-012:** Every ultraQA request to the SUT (MCP browser, http/db tools) shall traverse one egress guard. When the method is GET/HEAD and the host is allowlisted, allow it. When the method mutates (POST/PUT/PATCH/DELETE), deny it unless the route is on the explicit safe-write allowlist; a denylisted destructive route is always denied. Every denied request is recorded as a `blocked` qa_step, never forwarded. (The backend is unswaggered ~120 routes, hence default-deny on mutation plus a hard denylist for defense-in-depth.)
Destructive denylist (hard): `admin/add_balance`, `admin/subtract_balance`, `admin/extend_subscription`, `admin/edit_subscription`, `admin/expire_vless_license`, `admin/revoke_vless_license`, `admin/push`, `billing/topup`, `billing/add-card`, `DELETE billing/cards/:id`, `billing/renewal-payment`, `billing/landing-purchase`, `billing/webhook/*`, `billing/sync-payments`, `promo/activate`, `user/gift`, `user/claim-gift/:code`, `inventory/open-case`, `inventory/claim-daily`, `admin/giveaway/pick-winners`, `accounting/payouts`, `accounting/payouts/:id/complete`, support category/status deletes.
Source: ADR-0008 §(c). Impl: `ultraqa.guard` proxy (verb rule + allowlist + denylist). Test: `agents/ultraqa/tests/rules/test_br012_guard.py` (`@spec BR-012`) — an uncatalogued POST is denied; a denylisted route is denied and never forwarded; a safe GET passes.
Audit (must return 0 rows):

```sql
SELECT id FROM qa_steps WHERE forwarded = true AND verb <> 'GET' AND safe_write = false;
```

Related: BR-011, BR-013.

**BR-013:** ultraQA shall authenticate to the SUT as a seeded, disposable, non-admin user holding no admin JWT (so admin-API mutations are `403` by credential regardless of the guard), and any direct SUT DB connection shall set `default_transaction_read_only = on`.
Source: ADR-0008 §(c); SR-1/SR-2 (creds from env), SAFE-2. Impl: `ultraqa.settings` (SUT creds from env, BR-010); `db_readonly` tool. Test: `agents/ultraqa/tests/rules/test_br013_identity.py` (`@spec BR-013`) — the SUT session carries no admin claim; the read-only pool rejects a write.
Related: BR-010, BR-011, BR-012.

**BR-014:** Every ultraQA LLM turn shall go through `LLMClient` (`complete_with_tools`), keeping the per-request USD ceiling (BR-006); a sweep additionally caps cumulative LLM cost (BR-007) and ReAct steps. When cumulative cost would exceed the run cap OR step count hits `max_steps`, then the episode stops and records its partial result, making no further tool calls.
Source: ADR-0008; SAFE-3, BR-006, BR-007. Impl: `LLMClient.complete_with_tools` (per-call guard); `ultraqa.graph` ReAct loop (run cost cap + `max_steps`). Test: `agents/ultraqa/tests/rules/test_br014_loop_bounded.py` (`@spec BR-014`).
Related: BR-006, BR-007.

**BR-015:** A repeat observation of the same defect shall update the existing Finding (bump `count`, `last_seen`) rather than create a new row. `dedup_key = sha256(check \x00 target \x00 signature)[:32]`, `UNIQUE`, upserted (the monitoring `dedup_key` pattern); `signature` is the spec-ref/normalized-symptom, not a per-run timestamp.
Source: ADR-0008; mirrors BR-009 / monitoring incidents. Impl: `ultraqa.store.upsert_finding` (`ON CONFLICT (dedup_key)`). Test: `agents/ultraqa/tests/rules/test_br015_finding_dedup.py` (`@spec BR-015`).
Audit (must return 0 rows):

```sql
SELECT dedup_key FROM findings GROUP BY dedup_key HAVING count(*) > 1;
```

Related: BR-009, BR-016.

**BR-016:** ultraQA `Finding.severity` shall use the single cross-agent enum `{info, warning, critical}` — the values `agentkit.Alert` carries and the `notify` routing key (`f"{agent}.{severity}"`, AR-5) emits — so dashboard `?severity=` filters, alert routing, and audits agree. No `low/medium/high` variants.
Source: ADR-0008; AR-5, BR-009. Impl: `ultraqa.store` severity `CHECK (severity IN ('info','warning','critical'))`. Test: `agents/ultraqa/tests/rules/test_br016_severity_enum.py` (`@spec BR-016`).
Audit (must return 0 rows):

```sql
SELECT id FROM findings WHERE severity NOT IN ('info','warning','critical');
```

Related: BR-009, BR-015.

---

## Edge cases / exceptions

| Rule   | Exception         | Reason                                                                    |
| ------ | ----------------- | ------------------------------------------------------------------------- |
| BR-002 | role `ops`        | Dashboard operator reads cross-tenant by design (US-013)                  |
| BR-008 | `interval_s == 0` | An on-demand job has no `batch-overdue` rule — only status failures apply |

---

## Versioning

Changing a business rule is a decision → ADR + a dated `(vN)` entry archived under `## Archived rules` here, with `Replaced by` / `Reason`.

---

## Related docs

- [domain-model.md](domain-model.md) · [user-stories.md](user-stories.md) · [functional-decomposition.md](functional-decomposition.md) · [architecture.md](architecture.md) · [adr/](adr/)
