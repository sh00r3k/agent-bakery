# User Stories — agent-bakery

**Date:** 2026-06-15
**Type:** Behavioral spec. Each story is observable behavior with acceptance criteria.

---

## What this is

What the agents must _do_, viewed from each actor — not how they are built. IDs (US-NNN)
are referenced by [business-rules.md](business-rules.md),
[functional-decomposition.md](functional-decomposition.md), and the verification suite.
Entities live in [domain-model.md](domain-model.md).

Each story: **US-NNN** — As a `<role>`, I want X, so that Y. Then acceptance criteria as
Given/When/Then. Each carries Priority / Phase / Depends-on and a `@spec US-NNN` test trace.

---

## Roles (actors)

| Role         | Description                                                                                     |
| ------------ | ----------------------------------------------------------------------------------------------- |
| `ops`        | Dashboard operator across all agents; holds a minted ops JWT, may read cross-tenant on purpose. |
| `qa-owner`   | Owns the ultraQA tester: points it at a SUT, reviews and triages findings.                      |
| `maintainer` | A developer extending the platform (adding an agent, running the dashboard).                    |
| `system`     | Scheduled/automated processes (monitoring collectors, QA sweeps).                               |

---

## Epic 1: Agent operations & monitoring

### US-007: Ops sees agent health and incidents

As an ops user, I want one dashboard showing agent health and open incidents, so that I can run all the agents from a single console.

- Given the monitoring agent has scraped /healthz, /readyz, /metrics.json, Docker, host, RabbitMQ, when ops opens the dashboard, then it shows each agent's up/ready state and any firing Incidents

**must-have · Phase 1 · Depends on: US-011** · `@spec US-007`; dashboard panels (HTMX) + ops-JWT fan-out.

### US-011: Monitoring detects, dedups, and alerts on SLO breaches

As an ops user, I want SLO breaches turned into deduplicated alerts, so that I'm notified once per problem, not per tick.

- Given the monitoring agent finds an agent's /readyz failing on repeated ticks, when it evaluates rules, then Signals fold (by dedup_key) into one firing Incident and exactly one alert is published to RabbitMQ agent.alerts (BR-009)
- Given a firing Incident whose condition clears, then the Incident becomes resolved and no new alert storm is sent

**must-have · Phase 1 · Depends on: —** · `@spec US-011`, BR-009; monitoring collect→rules→classify→dedup→notify.

---

## Epic 2: Extensibility (maintainers)

### US-012: Maintainer adds a new agent on agentkit

As a maintainer, I want to scaffold a new agent reusing agentkit, so that I get config, LLM client, health endpoints, auth, and persistence for free.

- Given a new workspace member that subclasses BaseAgentSettings and calls create_app()
- When it boots
- Then /healthz, /readyz, /metrics.json respond and the monitoring agent can scrape it; no agentkit code or other agent's DB was modified (per-agent isolation)

**must-have · Phase 0 · Depends on: —** · `@spec US-012`; agentkit `create_app`, `BaseAgentSettings`. Ref: [add-your-own-agent.md](add-your-own-agent.md).

### US-013: Run the dashboard with any agent set

As a maintainer, I want the dashboard to run against any composition of agents via a config-driven registry, so that it works whether I deploy one agent or all.

- Given a dashboard config listing only the monitoring agent, when the dashboard starts, then only panels for present agents render; absent agents are skipped, not errored
- When the dashboard fetches an agent over HTTP, then it presents a freshly minted ops JWT (agentkit auth)

**must-have · Phase 1 · Depends on: US-012** · `@spec US-013`; dashboard agent registry + HTTP client.

---

## ultraQA: agentic QA (Epic, Phase 2)

The tester agent — drives a real product end-to-end to find defects the unit/integration/e2e
layers miss, using the SUT's own spec as the oracle. Governed by
[ADR-0008](adr/decisions.md) and BR-011…BR-016.

### US-014: ultraQA explores the SUT autonomously

As a QA owner, I want an agent that drives the product like a user (opening pages, clicking controls, exercising flows), so that defects no scripted test covers are surfaced.

- Given the ultraQA agent pointed at the target dev environment (BR-011)
- When a sweep runs
- Then it navigates the Mini-App via the MCP browser, exercises safe flows, records each visited route/page as a CoverageNode; console errors, 4xx/5xx, and broken states become Findings

**must-have · Phase 2 · Depends on: US-012, US-020** · `@spec US-014`; ultraqa explore node + MCP browser tools.

### US-015: ultraQA asserts behavior against the spec oracle

As a QA owner, I want observed behavior checked against the product spec (`userstory.md`/`QA_MANUAL.md` Given/When/Then), so that I find _correctness_ gaps, not just crashes.

- Given the SUT oracle docs are indexed (pgvector KB, US-021)
- When ultraQA exercises a flow with a spec expectation
- Then it asserts the observed outcome against the spec Given/When/Then; a divergence is recorded as a Finding carrying its spec_ref

**must-have · Phase 2 · Depends on: US-014, US-021** · `@spec US-015`; ultraqa assert node + `spec_lookup` tool.

### US-016: ultraQA is fenced to a fail-closed safety envelope

As a platform owner, I want the autonomous agent unable to touch prod or fire destructive/admin actions, so that 24/7 operation is safe.

- Given ultraQA with ENV=dev and a host:port allowlist (BR-011)
- When the model attempts any SUT request
- Then it traverses the guard proxy (BR-012): GETs to allowlisted hosts pass, mutating verbs are denied unless safe-write-allowlisted, denylisted routes are always blocked and recorded; the SUT session is a non-admin disposable user (BR-013); starting with ENV!=dev or an unmatched target refuses to boot

**must-have · Phase 2 · Depends on: —** · `@spec` BR-011/012/013; `ultraqa.guard` egress proxy + start-check.

### US-017: Findings are deduped and reported to the dashboard + alerts

As an ops user, I want ultraQA findings on the dashboard and (when severe) as alerts, deduped across sweeps, so that I see each defect once.

- Given ultraQA detects a defect
- Then it upserts a Finding by dedup_key (BR-015), severity in {info,warning,critical} (BR-016); GET /findings serves it to the dashboard (features:["findings"]); a critical Finding publishes one Alert to RabbitMQ agent.alerts (AR-5, BR-009)

**must-have · Phase 2 · Depends on: US-013** · `@spec US-017`, BR-015/016; ultraqa store upsert + `/findings` + notify.

### US-018: ultraQA tracks coverage

As a QA owner, I want to know what's been exercised vs not, so that sweeps target gaps instead of re-walking the same screens.

- Given completed sweeps
- Then each route/page is a CoverageNode (unexplored|explored|blocked); /metrics.json custom.coverage_pct reports explored / (explored+unexplored); destructive nodes are marked blocked, not counted as failures

**should-have · Phase 2 · Depends on: US-014** · `@spec US-018`; ultraqa coverage_nodes + metrics.

### US-019: ultraQA runs 24/7 and on demand

As an ops user, I want sweeps on a schedule and a manual trigger, so that regressions are caught continuously and I can re-run after a fix.

- Given ultraQA is deployed
- Then APScheduler runs an explore-sweep every poll interval; POST /scan triggers an on-demand sweep with identical behavior

**should-have · Phase 2 · Depends on: US-014** · `@spec US-019`; ultraqa scheduler + `/scan`.

### US-020: Tool-calling on the one shared LLM seam

As a maintainer, I want tool-calling added to `agentkit.LLMClient` (not a new client), so that the ReAct loop stays cost-metered on the shared seam (CR-3, SAFE-3).

- Given a model response with tool_calls
- When ultraQA calls LLMClient.complete_with_tools(messages, tools=...)
- Then it returns tool_calls + content + finish_reason (not just text); the per-request USD ceiling still applies (BR-006), Usage accumulates; the existing complete() contract is unchanged (monitoring unaffected)

**must-have · Phase 2 · Depends on: —** · `@spec US-020`, BR-006; `agentkit.llm.LLMClient.complete_with_tools`.

### US-021: ultraQA remembers across runs

As a QA owner, I want the agent to persist findings/coverage and index the SUT oracle, so that it dedups defects and grounds tests over time (not stateless).

- Given repeated sweeps
- Then findings/coverage persist in ultraQA's own Postgres+pgvector DB (AR-3); the SUT oracle docs are embedded for retrieval (EMBED_MODEL, vector(768)); its own checkpointer persists in-episode ReAct state in its own Postgres

**should-have · Phase 2 · Depends on: US-014** · `@spec US-021`; ultraqa pgvector KB + own checkpointer.

---

## Epic 3: Distribution & releases

### US-022: Tagged release publishes prebuilt, attested images

As a maintainer, I want a pushed SemVer tag to publish per-member images to GHCR, so that an operator can pin a concrete version with `IMAGE_TAG=0.2.0 docker compose pull && up -d` without cloning the repo or building from source.

- Given a tag `v0.2.0` is pushed
- When the release workflow runs
- Then per-member images appear in `ghcr.io/<owner>/agent-bakery/<member>:0.2.0` (matrix: `monitoring`, `dashboard`, `platform-cli`), each carrying an SBOM + provenance attestation; no `:latest` tag is ever published (ADR-0009)
- Given a base image is digest-pinned in the per-member Dockerfile, when the image is rebuilt from the same tag, then the produced digest is identical
- Given an operator pulls `IMAGE_TAG=0.2.0`, when they run `docker compose pull`, then no per-service `build:` stanza runs — the registry is the single source of truth for image content
- Given an operator runs `docker run --rm ghcr.io/<owner>/agent-bakery/platform-cli:0.2.0 doctor --json`, when the platform stack is healthy, then the CLI prints one JSON line per registered agent's `/healthz` + `/readyz` and exits 0

**must-have · Phase 1 · Depends on: ADR-0009** · `@spec US-022`; `.github/workflows/release.yml`, per-member Dockerfile hardening (digest-pinned, non-root, lockfile-faithful), ADR-0012.

---

## Out-of-scope stories

- **NS-002: Own end-user login / identity database** — tenants own identity; the platform verifies JWTs (agentkit `auth`), it does not run a login. Aligns with the platform's hard scope boundary.
- **NS-003: Adopt LangGraph Platform/Server** — [ADR-0001](adr/decisions.md): library, not platform.

---

## Story sizing

| Size | LoE  | When            |
| ---- | ---- | --------------- |
| XS   | <1d  | trivial         |
| S    | 1-2d | normal feature  |
| M    | 3-5d | multi-component |
| L    | 1-2w | needs discovery |
| XL   | >2w  | decompose first |

---

## Related docs

- [domain-model.md](domain-model.md) · [business-rules.md](business-rules.md) ·
  [functional-decomposition.md](functional-decomposition.md) · [architecture.md](architecture.md)
