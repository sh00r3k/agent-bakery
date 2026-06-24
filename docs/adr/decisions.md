# Architecture Decision Records

An append-only log of architectural decisions — the context, the options weighed,
and the trade-offs accepted, so six months on we still know _why_.

**Status values:** proposed → accepted → superseded (by ADR-NNNN) / rejected.
ADRs are never deleted, never renumbered; only the status changes.

**Write one when:** choosing a framework/datastore/protocol, breaking a public
API or the agentkit contract, changing an iron rule, or making a long-lived
trade-off. **Not for:** bug fixes, refactors, style calls, simple features.
Product/scope pivots go in `agents/<agent>/docs/decision-log.md`.

Related: [architecture.md](../architecture.md) · [business-rules.md](../business-rules.md) · [roadmap.md](../roadmap.md)

---

## ADR-0001 — Embed LangGraph as a library, run your own infra layer

_Accepted 2026-06-13_

LangGraph Server/Platform is a commercial product (license key + LangSmith key,
startup license validation, egress to a LangChain beacon). The OSS `langgraph`
library is Apache-2.0 with no keys.

**Decision:** do **not** adopt LangGraph Server/Platform. Each agent embeds the
OSS library in its own FastAPI/CLI process; the shared "instance" is a self-hosted
**infra layer** (Postgres+pgvector, Redis, RabbitMQ, Ollama) plus one Python
toolkit (`agentkit`) and a reverse proxy.

**Trade-off:** $0/OSS/no egress, independent deploy per agent — but no Studio /
managed runtime, and cross-agent calls are plain HTTP/AMQP, not `RemoteGraph`.
_Revisit if_ you outgrow one host or LangChain ships a free production server.

## ADR-0002 — Dashboard: HTMX over a React SPA

_Accepted 2026-06-15_

`apps/dashboard` is an internal ops console for a handful of operators; its data
is already produced server-side and interactivity is modest (US-007/US-013).

**Decision:** server-rendered HTML + **HTMX** (htmx polling for live panels); no
SPA, no client JSON API, no JS build step. The ops JWT is minted server-side and
never reaches the browser.

**Trade-off:** one toolchain, smaller attack surface, config-driven registry
renders any agent set — but heavy client-side UX (offline, drag-drop) would be
awkward. _Revisit if_ it becomes a customer-facing multi-user product. Refs: ADR-0004.

## ADR-0003 — LLM model-tier mapping via the gateway

_Accepted 2026-06-15_

Agents make cheap high-volume calls (classify/triage) and slower quality calls
(summarization/reasoning). Hardcoding model IDs causes provider lock-in and scatters
cost reasoning (BR-006, BR-007).

**Decision:** agents request a **logical tier** (`fast`/`quality`) via config;
`LLMClient` sends the tier-mapped model to the OpenAI-compatible gateway, which
owns the concrete model/provider. The USD meter prices per tier. Concrete model
names live in gateway config / `env.example`, never in source.

**Trade-off:** provider-agnostic, uniform cost ceilings — but one indirection
layer that must stay in sync with the gateway. Embeddings stay separate
(Ollama `nomic-embed-text`, `vector(768)`). Refs: ADR-0001.

## ADR-0004 — agentkit: vendored copy → uv workspace

_Accepted 2026-06-15_

Every member imports `agentkit`. The first scaffolds vendored a copy per agent,
which immediately drifted (fixes applied N times, divergent versions).

**Decision:** a **`uv` workspace** — `packages/agentkit` is the single canonical
toolkit, a path member of each agent/app, one `uv.lock` at the root. Each member
keeps its own `pyproject.toml`/Dockerfile/deploy unit, so independent deploy
(ADR-0001) is preserved.

**Trade-off:** one source of truth, reproducible lock, fast local dev — but a
shared resolver means a toolkit dep bump touches all members, and the repo is tied
to `uv`. _Revisit if_ agentkit must be consumed outside this repo → publish it.

## ADR-0007 — Observability: structlog JSON + optional OpenTelemetry

_Accepted 2026-06-20_

The host is small and tracing rarely earns its RAM, so the baseline is
structured JSON logs (structlog). Tracing is **optional OpenTelemetry**
(`OTEL_EXPORTER_OTLP_ENDPOINT`, off by default) via the agentkit
`observability` extra, pointed at a shared collector per-agent only where the
trace value justifies the cost. No bundled tracing service or trace database.

**Trade-off:** zero-overhead default, no extra service to run — but no
turnkey trace UI; bring your own collector if you enable OTel.

## ADR-0008 — ultraQA: the first tool-using / ReAct agent

_Accepted 2026-06-18_

Existing members are reactive/inbound and never let the LLM call tools. **ultraQA**
exercises a real product end-to-end (drive a browser, click flows, read results,
decide next step) against an external SUT (the target dev environment only). This forces
four bindings the iron rules govern but don't yet resolve.

**Decision** — new uv member `agents/ultraqa` on `agentkit`, with:

- **(a) Tool-calling by extending `LLMClient`** (additive, CR-3): a
  `complete_with_tools(...) -> (ToolTurn, Usage)` method surfaces `tool_calls` /
  `finish_reason` that `complete()` drops, keeping the same USD cost guard
  (BR-006, SAFE-3). No second client; ReAct loop is an OSS `StateGraph` in-process
  (AR-1, ADR-0001). `complete()` untouched (AR-2).
- **(b) Real browser via MCP-as-a-library** (`langchain-mcp-adapters`): Playwright
  MCP + Chrome DevTools MCP loaded as LangChain tools inside our process — a tool
  transport, not a RemoteGraph deployment (AR-1 holds).
- **(c) Outbound fenced by a network-path egress-guard proxy** (BR-011/BR-012,
  AR-4): a single chokepoint every byte traverses (browser launched with
  `--proxy-server` at the guard, plus probe/db tools); dev-контур host:port
  **allowlist** (container DNS, fail-closed, refuses unless `ENV=dev`);
  **default-deny on every mutating method** + a destructive-route denylist;
  a **disposable non-admin** SUT identity (BR-013); read-only SUT DB plane. An
  in-process denylist is defense-in-depth only — the boundary is on the network
  because the model drives a Chromium it doesn't own. Autonomous loop is
  read + safe-write only; destructive nodes need a separately-gated human action.
- **(d) Its own Postgres+pgvector DB and Redis index** (AR-3) — own tables (runs,
  steps, findings) + a pgvector KB over SUT oracle docs; exposes the standard
  agentkit surface plus `GET /findings` (severity enum `{info, warning, critical}`,
  AR-5/BR-016) so the dashboard renders it as a `features:["findings"]` agent.

**Trade-off:** a ReAct capability on the _same_ agentkit contract, prod and
money/license routes unreachable by construction, data isolation preserved — but
new heavy deps (MCP servers, Chromium), a non-deterministic token-hungry loop
needing step/turn + cost caps (BR-006/007), and a security-load-bearing
allow/denylist that must be tested and kept in sync. _Revisit if_ a second SUT is
added, tool-calling becomes cross-agent, or a managed runtime is ever needed
(supersede ADR-0001 first).

_Rejected:_ a separate tool-calling client (forks the cost guard, breaks AR-2/CR-3);
LangGraph Server/RemoteGraph (violates AR-1/ADR-0001); MCP as a remote platform;
raw Playwright without MCP; sharing monitoring's DB (AR-3); an in-process-only
envelope (bypassed by page-originated requests); an admin SUT identity (one missed
verb-rule mutates real state).

## ADR-0009 — Ship agent-bakery as a root-compose distribution

_Proposed 2026-06-20_

`deploy-your-own.md` is a manual per-member build/run checklist with load-bearing
gotchas (network-first ordering, exact container names, repo-root build context,
shared `JWT_SECRET`). Adopters want install-the-platform-in-one-command. Constraints:
ADR-0001 (no LangGraph Platform), NS-002 (host owns identity — no bundled login),
BR-010 (no secrets in repo). See [design-distribution.md](../design-distribution.md).

**Decision:** add a repo-root `docker-compose.yml` that _owns_ the `agent_backend`
network, brings up shared infra (Postgres+pgvector, Redis, RabbitMQ) plus the
**real** members (`monitoring`, `dashboard`), runs `infra/bootstrap.sql` on first
boot, and gates Caddy / docker-socket-proxy behind `edge` / `meta` profiles.
Services default to pulling `${REGISTRY}/<svc>:${IMAGE_TAG}` (one
platform tag per the architecture.md versioning policy) with an opt-in `build:`
override. A root `env.example` carries only compose-interpolated infra knobs; app
secrets stay in per-member `.env`. The first ops/dev JWT is minted host-side via
`apps/dashboard/scripts/mint-admin-token.py` — no bundled identity store.

**Trade-off:** gain one-command install, reproducible pinned releases, optional
pieces isolated, identity stays host-owned — but need a release pipeline to
build+push images, a versioning discipline (never `latest` in prod), and a
documented footgun: `bootstrap.sql` runs only on a fresh pgdata volume.
_Revisit if_ multi-host scale-out
(architecture.md open question 1) outgrows single-host compose.

## ADR-0010 — The platform exposes no MCP surface of its own

_Accepted 2026-06-20_

The platform does **not** expose its agents over MCP — there is no MCP server
surface in agentkit and none is shipped. MCP stays a purely _inbound_ tool
concern: an agent may call external MCP servers as LangChain tools (ADR-0008,
ultraQA browser tooling), but no member publishes an MCP endpoint.

**Trade-off:** smaller surface and no extra auth/tenant boundary to maintain —
_revisit_ if an external MCP client must drive the platform's agents directly.

## ADR-0011 — Operator `platform` CLI owns the registry config key as single source of truth

_Accepted 2026-06-20_

_Implemented in this change set: `apps/platform-cli`._

Attaching an agent today means hand-editing `infra/bootstrap.sql`, an `env.example`,
and dashboard config ([add-your-own-agent.md] §5). The dashboard registry is already
fully config-driven (US-013): `settings.agents` is parsed from a `DASHBOARD_AGENTS`
JSON env var and absent agents degrade gracefully, so "register an agent" is a pure
config write needing no dashboard code change. We want a thin CLI that edits exactly
that key without becoming a parallel control plane or a second schema. See
[design-platform-cli.md](../design-platform-cli.md).

**Decision:** ship `apps/platform-cli` as a uv workspace member with a `platform`
console-script. It reads/writes **only** the `DASHBOARD_AGENTS` key, imports
`dashboard.settings.AgentConfig` + `registry.build_registry` (never re-declares
them), round-trips every write through the dashboard's own parser, and _stages_
changes that take effect on `platform up dashboard` (registry is read once at
startup — no hot reload). `token mint` shells the existing `mint-admin-token.py`
(roles `admin|manager`); `up/down` shell plain `docker compose` (repo-root file,
`--profile` pass-through for edge/meta); `doctor` probes each agent's
`/healthz`+`/readyz`. No identity store (NS-002), tenant stays a JWT claim (BR-002),
`JWT_SECRET` read from env only, never written (BR-010).

**Trade-off:** gain a one-command operator UX with zero dashboard code change and a
registry contract with exactly one writer and one schema — but on-disk config can
briefly differ from the running dashboard until `up dashboard`, and the CLI takes a
workspace dependency on the dashboard package for its config models. DB bootstrap
and CI wiring stay manual (the CLI replaces only the dashboard-registration step).
_Revisit if_ a live `/reload` endpoint becomes necessary, or the registry must
mutate without a restart.

## ADR-0012 — Release pipeline: tag `v*` → per-member images to GHCR

_Proposed 2026-06-21_

ADR-0009 ships the platform as a root-compose distribution with
`${REGISTRY}/<svc>:${IMAGE_TAG}` placeholders, but the _release pipeline_ —
what actually builds and pushes those images — is still hand-rolled. Operators
want `IMAGE_TAG=0.2.0 docker compose pull`; today they must clone the repo
and `docker build` per service. Constraints: BR-010 (no secrets in repo),
ADR-0001 (OSS-only infra, no LangGraph Platform), ADR-0004 (uv workspace → one
lockfile per member).

**Decision:** add `.github/workflows/release.yml` triggered by push of
`v[0-9]+.[0-9]+.[0-9]+` tags. One job, matrix `{monitoring-agent, dashboard,
platform-cli}`; each leg builds the per-member Dockerfile with the repo root
as build context (`uv sync --frozen --no-dev --package <member>`), and pushes
to `ghcr.io/${{ github.repository_owner }}/agent-bakery/<member>:${VERSION}`
with **BuildKit provenance + SBOM** (`provenance: true`, `sbom: true` on
`docker/build-push-action@v6`). No `:latest` — ADR-0009 calls it a footgun in
prod; operators pin a concrete version and roll forward deliberately. Cache:
GitHub Actions cache (`type=gha`); base images stay digest-pinned per the
existing Dockerfile pattern, so re-builds of the same tag reproduce the same
digest. New `apps/platform-cli/Dockerfile` (CLI, not a long-running service)
joins the matrix — `platform-cli` has **no compose service**, just a
publishable binary image (`CMD ["platform", "--help"]`) so operators can run
`docker run --rm …/platform-cli:0.2.0 doctor --json` against a live stack.
Auth uses the built-in `GITHUB_TOKEN` against GHCR (no extra secrets).

**Out of scope — and why:**

- `examples/hello-agent` — 40-line teaching script, no `pyproject.toml`,
  intentionally local-only per its README; Dockerizing it adds noise and a
  publishable image of a file meant to be copied and edited.
- `examples/classifier-agent` — reference agent, **not** a workspace member
  (its `pyproject.toml` says so); per its README, "promoting it" requires
  moving it under `agents/`. Out of scope here.
- PR-validation workflow — out of scope (per-member `pytest` + `ruff` + `mypy`
  already gate correctness in CI on PRs).
- Multi-arch (`linux/arm64`) — out of scope per current ops (amd64 hosts
  only); add later via BuildKit `platforms:` + QEMU when needed.

**Trade-off:** gain one-command installs of prebuilt, attested images pinned
by digest — but need a GitHub repo (already have one), a release discipline
(SemVer tags, never `latest`), and an explicit owner for the GHCR namespace.
_Revisit if_ a private registry is required (swap `ghcr.io` → Harbor, keep
the workflow shape), or if multi-arch becomes a real operator requirement.
