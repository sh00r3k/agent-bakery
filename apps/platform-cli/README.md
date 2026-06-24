# `platform` — operator CLI

A thin operator CLI for the agent platform (ADR-0011 / ADR-0009). It is **not a
control plane**: it writes config and restarts processes, holds no state, and
never talks to a running dashboard.

## What it owns

The single source of truth for the agent registry is the **`DASHBOARD_AGENTS`**
key in `apps/dashboard/.env` — a JSON array consumed by
`dashboard.settings.Settings.agents` (US-013, config-driven registry). The CLI
owns **only that one key**; it never touches `JWT_SECRET`, `LLM_*`, or any other
env. It imports the dashboard's `AgentConfig` / `build_registry` rather than
re-declaring the schema, so it can never become a second source of truth.

## Install

```bash
uv sync            # registers this workspace member; the `platform` script lands on PATH
```

## Commands

| Command | What it does |
| --- | --- |
| `platform up [svc…] [--profile observability\|edge\|meta]` | `docker compose up -d` from the repo root (default compose); `--profile` adds opt-in members |
| `platform down [svc…] [--profile …]` | `docker compose down` (keeps volumes) |
| `platform agent add <slug> --url <u> [--kind server\|batch\|self] [--port N] [--title T] [--feature F …]` | Append/replace an agent in `DASHBOARD_AGENTS` |
| `platform agent list [--json]` | Print the registry as the dashboard will build it |
| `platform agent remove <slug>` | Drop the entry with that slug |
| `platform token mint [--sub S] [--tenant T] [--role admin\|manager] [--ttl N] [--audience A]` | Mint an operator JWT (shells `mint-admin-token.py`) |
| `platform doctor [--slug S] [--json]` | Probe `/healthz` + `/readyz` per registered agent |

## Key contracts

- **Staged, not applied.** The dashboard reads the registry once at start, so
  `agent add/remove` rewrites config and prints
  *"run `platform up dashboard` to apply"*. There is no live-reload path.
- **Atomic single-key write.** The `.env` is rewritten via a temp-file +
  atomic rename, touching only `DASHBOARD_AGENTS`; every other line is byte-for-byte
  preserved.
- **Features validated.** `--feature` is checked against
  `registry.KNOWN_FEATURES` (`incidents, findings, coverage, runs, pm`); an
  unknown feature is rejected at the CLI. `--kind` is validated by `AgentConfig`.
- **Tokens.** `token mint` reads `JWT_SECRET` from the environment (never an
  arg, never written anywhere); with no secret it exits non-zero and prints
  nothing to stdout. Only `admin` / `manager` roles are offered.
- **Compose stays OSS.** `up`/`down` shell plain `docker compose` per ADR-0001 —
  never a managed runner.

## Out of scope

DB provisioning (stays a `bootstrap.sql` step — the CLI only reminds), a live
`/reload` endpoint, an identity/login store, and minting `ops`/`end-user`
tokens.
