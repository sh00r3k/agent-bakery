# The agent-bakery agent standard

**One shape for every agent.** A monorepo member (`agents/monitoring`), an
extracted standalone repo (`ultraQA`), and your next agent all
follow the same contract, so the platform — the `monitoring` meta-monitor and
the `apps/dashboard` ops console — can observe and drive any of them without a
single line of agent-specific code.

This doc is the **contract**. For a copy-paste quickstart see
[`add-your-own-agent.md`](add-your-own-agent.md); the reference implementation is
[`agents/monitoring`](../agents/monitoring).

---

## 1. Two deployment shapes, one package

Every agent is a Python package built **on `agentkit`**. The only difference
between an in-repo member and a standalone repo is how it pulls `agentkit`:

| Shape | When | `agentkit` source | Example |
| --- | --- | --- | --- |
| **Workspace member** | generic / OSS, lives in this monorepo under `agents/<name>` | `agentkit = { workspace = true }` | `agents/monitoring` |
| **Standalone repo** | product-specific, private, or independently released | vendored: `agentkit = { path = "vendor/agentkit" }` | `ultraQA` |

Nothing else changes — same layout, same endpoints, same registration. An agent
can be **promoted** from member → standalone (see §6) without touching its code.

---

## 2. Filesystem layout (identical for both shapes)

```
<agent>/
├── pyproject.toml          # name, deps (agentkit + langgraph), build, tool config
├── env.example             # every env var, with safe defaults
├── README.md               # what it does, surface, safety model
├── Dockerfile              # one stage, runs `python -m <pkg>`
├── docker-compose.yml      # reference compose (joins the shared agent network)
└── src/<pkg>/
    ├── __init__.py
    ├── settings.py         # class Settings(BaseAgentSettings)
    ├── graph.py            # the compiled LangGraph (or batch logic)
    ├── api.py              # create_app(Settings()) + feature routes
    ├── __main__.py         # main(): uvicorn for servers, scheduler for batch
    ├── store.py            # Postgres access (the agent OWNS its DB)
    └── scheduler.py        # APScheduler jobs, if any
└── tests/                  # domain / rules (BR-*) / stories (US-*)
```

A **standalone** repo additionally carries what the monorepo root used to
provide: its own `.gitignore`, `LICENSE`, `.github/workflows/ci.yml`, and the
`[tool.ruff]` / `[tool.mypy]` / `[tool.pytest.ini_options]` blocks in its
`pyproject.toml` (these are inherited from the workspace root for members, so a
member's `pyproject.toml` omits them).

---

## 3. The platform contract — how an agent "plugs in"

An agent is observable/drivable by the platform iff it exposes these. All come
from `agentkit.create_app()` for free unless noted.

### 3.1 Lifecycle endpoints (every agent, mandatory)

- `GET /healthz` — liveness (process up).
- `GET /readyz` — readiness (deps reachable: DB, redis, gateway).
- `GET /metrics.json` — the agentkit metrics snapshot the monitoring agent
  scrapes: per-agent request counts, latencies, LLM cost, last-run heartbeat.

### 3.2 Feature endpoints (declare what you have)

The dashboard renders panels purely from an agent's declared `features`. Each
feature is a capability name bound to a known endpoint shape:

| `feature` | Endpoint(s) the agent must serve | Dashboard panel |
| --- | --- | --- |
| `incidents` | `GET /incidents?limit=` | Incidents |
| `findings` | `GET /findings?severity=` · `POST /findings/resolve` | Findings / QA |
| `runs` | `GET /runs` | Pipeline |
| `pm` | `GET /pm` | PM |

Serve only the features you declare; unknown feature names are ignored. The
`findings` resolve channel is how an operator's triage decision flows back — the
agent **owns its finding state**, the dashboard never writes the agent's DB.

### 3.3 Async alerts (optional)

Publish to the RabbitMQ `agent.alerts` topic for the monitoring agent to triage.
Same envelope as `agents/monitoring` consumes.

---

## 4. Registration — zero code, pure config

Adding an agent to a running platform is two env entries, no redeploy of the
platform:

1. **Dashboard** — append to `DASHBOARD_AGENTS` (JSON array; see
   `apps/dashboard/src/dashboard/settings.py`):
   ```json
   {"slug":"my-agent","url":"http://my-agent:8000","kind":"server","features":["findings"]}
   ```
   `kind`: `server` (always-on HTTP), `batch` (no port; freshness from last-run
   heartbeat), or `self` (the dashboard tile).

2. **Monitoring** — add the agent's URL to the monitoring agent's
   `agent_endpoints` so it scrapes `/metrics.json` and SLO-checks it.

3. **Infra** — add the agent's Postgres DB to `infra/bootstrap.sql` (each agent
   owns its own DB; agents never read each other's Postgres).

The default registries in this repo list a *representative* set of agents
(security, pm, …) most of which are external — they are examples, not
requirements. The platform imposes no requirement that any particular agent be
present.

---

## 5. SUT-acting agents — the safety capability (optional)

An agent that performs **outbound mutating actions against an external system**
(like ultraQA driving a SUT) must add a fail-closed envelope, enforced on the
network path, not on model behavior. The pattern (BR-011…016, ADR-0008):

- **Dev-only.** Refuse to start unless `ENV=dev` and every target is in an
  explicit `SUT_ALLOWLIST` (host:port).
- **Egress guard.** Route every tool request through a guard proxy; reads pass,
  mutating verbs are denied by default, a destructive denylist is always blocked.
- **Disposable non-admin identity;** read-only DB connections.
- **Cost/step bounded** via `agentkit.LLMClient` per-call USD ceiling + per-run
  cap + `max_steps`.

This is a capability, not a requirement — pure read-only agents (monitoring)
don't need it.

---

## 6. Extracting a member into its own repo

When an agent becomes product-specific or private, promote it. This is exactly
how `ultraQA` was lifted out:

1. Copy `agents/<name>/` to a new repo root (drop `__pycache__`, caches,
   `node_modules`).
2. Vendor the toolkit: copy `packages/agentkit` → `vendor/agentkit`; set
   `[tool.uv.sources] agentkit = { path = "vendor/agentkit", editable = true }`.
3. Carry the tool config the workspace root provided: add `[tool.ruff]`,
   `[tool.mypy]`, and `[tool.pytest.ini_options]` (`asyncio_mode="auto"`,
   `pythonpath=["src"]`, `testpaths=["tests"]`), plus a `[dependency-groups] dev`.
4. Add `.gitignore`, copy `LICENSE`, add a `.github/workflows/ci.yml`.
5. In the monorepo: `git rm -r agents/<name>`; remove its lines from the root
   `pyproject.toml` (`[tool.uv.sources]` + `dev` deps) and the CI matrix; keep a
   representative registry entry in the dashboard if it's still one of your agents.
6. `uv sync && uv run pytest tests` in the new repo must be green with **no
   sibling checkout** — that is the self-contained bar.

A standalone repo stays plug-compatible with the platform: same endpoints, same
`DASHBOARD_AGENTS`/monitoring registration. The platform doesn't care where the
code lives.
