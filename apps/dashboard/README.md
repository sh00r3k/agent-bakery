# dashboard

Unified, server-rendered ops console for your agents. A peer agent built on
`agentkit`: FastAPI + Jinja2 + HTMX + httpx. It is an **HTTP client of every
other agent** (never reads their DBs) and owns only its own small DB `dashboard`
(heartbeat + cost time-series).

## Config-driven agents (no hardcoded agents)

The dashboard observes **whatever agents you declare** — any composition, from
zero to N. The registry comes entirely from `DASHBOARD_AGENTS`, a JSON array
(see `env.example`); nothing in the code is wired to a specific agent. Each
entry:

| key        | required | meaning                                                        |
| ---------- | -------- | -------------------------------------------------------------- |
| `slug`     | yes      | stable id (used in URLs / cache keys)                          |
| `url`      | yes      | in-cluster base URL (service hostname over the shared network) |
| `kind`     | no       | `server` \| `batch` \| `self` (default `server`)               |
| `title`    | no       | display name on the tile (default: `slug`)                     |
| `port`     | no       | host loopback port shown on the tile (default `0`)             |
| `features` | no       | capabilities → which panels/links render (see below)           |

Recognized **features**: `incidents`, `findings`, `coverage`, `runs`, `pm`.
Panels and overview-tile links render **per declared capability**, not per
agent name — so an agent slugged `watchdog` with `features: ["incidents"]`
drives the Incidents screen just as well as one named `monitoring`. A screen
whose capability no agent declares degrades gracefully ("no *X*-capable agent in
registry") instead of erroring.

## Screens (left nav, HTMX-polled partials)

- **Agent overview** `/` — one tile per declared agent, color-coded from
  `/healthz` + `/readyz` + `/metrics.json`; aggregate banner (healthy/degraded/
  down, open incidents, high findings, today's LLM spend). Refresh 10s.
- **Incidents** `/incidents` — the incidents-capable agent's `GET /incidents`;
  **Run sweep now**.
- **Findings** `/findings` — the findings-capable agent's `GET /findings`;
  **Run scan now**.
- **Pipeline** `/pipeline` — a runs-capable (batch) agent's `GET /runs` table;
  **Run pipeline** → `POST /actions/webext/run` forwards `POST /run` (limit=1),
  shows the returned `run_id`, a benign "already in progress" notice on 409, and
  an offline tile when unreachable.
- **PM digests** `/pm` — a pm-capable agent's `/digests` + `/action-items`.
- **LLM cost** `/cost` — per-agent today's spend aggregated from `/metrics.json`.

## Auth

Admin-only. `GET /login` accepts a host-minted HS256 admin token (see
`scripts/mint-admin-token.py`), verifies it via `agentkit.auth`, and stores it in
an HttpOnly/Secure/SameSite=Strict cookie. The dashboard mints its **own**
service admin token (same secret, `sub=dashboard`) for upstream agent calls.
Put a TLS-terminating reverse proxy (+ optional IP allowlist) in front in prod.

## Run

```bash
cp env.example .env     # set JWT_SECRET, POSTGRES_PASSWORD, DASHBOARD_AGENTS
uv run python -m dashboard   # serves on $PORT (container 8000 -> host loopback)
```

`agentkit` is consumed as a uv **workspace** dependency from `packages/agentkit`
(no vendoring): `uv sync` at the repo root builds one editable env for the whole
monorepo.

## Test

```bash
uv run --project apps/dashboard pytest -q   # offline: mock httpx agents, no DB/network
uv run --project apps/dashboard ruff check src tests
```
