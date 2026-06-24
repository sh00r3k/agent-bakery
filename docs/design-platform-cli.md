# Design — `platform` operator CLI

**Date:** 2026-06-20
**Status:** Implemented 2026-06-20 — `apps/platform-cli/`
**Refs:** US-013 (config-driven registry / absent-panel skip), NS-002, BR-002, BR-010, ADR-0001;
`apps/dashboard/src/dashboard/settings.py` (`AgentConfig`, `DASHBOARD_AGENTS`, `AliasChoices`),
`apps/dashboard/src/dashboard/registry.py` (`build_registry`, `by_slug`, `with_feature`, `KNOWN_FEATURES`),
`apps/dashboard/scripts/mint-admin-token.py`,
`apps/dashboard/src/dashboard/aggregator.py` (`/healthz` `/readyz` probing),
`infra/bootstrap.sql`, `infra/env.example`, `docs/deploy-your-own.md`, `docs/add-your-own-agent.md` §5.

## Why

The current "add your own agent" flow (`docs/add-your-own-agent.md` §5 — *Wire
it up*) is hand work spread across four steps: add the agent's DB to
`infra/bootstrap.sql`, add an `env.example`, hand-register it in `apps/dashboard`
settings, and add it to the CI matrix. An operator who just wants to *attach an
already-deployed agent and mint a login token* has to know the JSON shape of
`DASHBOARD_AGENTS`, the `mint-admin-token.py` flags, and the docker-compose
lifecycle. That is too much surface for a thin operation. (The CLI replaces only
the **dashboard-registration** step — DB bootstrap and CI wiring stay manual; see
§Scope non-goals and the Docs implementation step.)

The seam to build on already exists and is **load-bearing**: the dashboard
registry is fully config-driven (US-013) — `settings.agents` is parsed from the
`DASHBOARD_AGENTS` env var (JSON array of `AgentConfig`), and absent agents
degrade gracefully instead of crashing. So "register an agent" is *purely* a
config write — **no dashboard code change** is needed. The `platform` CLI is a
thin, opinionated wrapper that edits exactly that one config key and drives the
compose lifecycle, so the operator never hand-edits JSON.

**Non-goal:** the CLI is not a control plane. It does not talk to a running
dashboard, mutate a live registry, or hold any state of its own. It writes
config and restarts processes. That keeps it from becoming a second source of
truth (see §Registry-write contract).

## Scope

In scope:

- `platform up` / `platform down` — wrap the repo-root `docker-compose.yml`
  (the default compose) that brings up core infra + the 3 agents on network
  `agent_backend`. Bare `platform up` == **core only**; opt-in members
  (edge-Caddy, docker-socket-proxy) come up via `--profile`
  (`edge` / `meta`), matching the compose's profile stanzas.
- `platform agent add | list | remove` — read/write the dashboard's
  `DASHBOARD_AGENTS` so the registry picks the agent up on next start (US-013).
- `platform token mint` — operator JWT (`admin`|`manager`) via the existing
  `mint-admin-token.py` (HS256, shared `JWT_SECRET`). The script's `--role`
  choices are exactly `admin`/`manager`; minting an `ops`/`end-user` role is
  **not** offered (it would require changing `mint-admin-token.py`'s `choices`,
  out of scope for a thin wrapper).
- `platform doctor` — probe each registered agent's `/healthz` + `/readyz`.

Out of scope (explicit non-goals):

- ❌ A live `/reload` endpoint on the dashboard. The registry is read **once**
  at `apps/dashboard/src/dashboard/api.py:72` (`build_registry(settings)` at
  module load); changing `DASHBOARD_AGENTS` after start has no effect without a
  restart. `agent add/remove` therefore **rewrites config + asks the operator to
  `platform up` the dashboard** — it never pokes a running process.
- ❌ An identity / login store. The CLI signs JWTs with the shared secret and
  exits; it never persists users or sessions (NS-002).
- ❌ A second copy of the `AgentConfig` schema, validation, or feature taxonomy.
  The CLI imports the dashboard's models; it does not redefine them
  (§Registry-write contract).
- ❌ DB provisioning. Creating the agent's database stays a `bootstrap.sql` step;
  the CLI only *reminds* (it does not run privileged SQL — sudo/DB ownership is
  the operator's, per the host's no-passwordless-sudo posture).

## Layer placement

Per `docs/architecture.md` layers, the CLI is a **Layer 3 workspace member** (an
app, like `apps/dashboard`), not part of Layer 2 `agentkit` and not infra. It
depends *down* on the dashboard's config models (Layer 3 peer import is allowed
only for the **declarative config contract**, never for the dashboard's runtime
internals — see the contract rule below) and shells out to Layer 1 (compose) and
to the existing `mint-admin-token.py` script.

```
apps/platform-cli/
├── pyproject.toml          # name="platform-cli"; [project.scripts] platform = "platform_cli.__main__:main"
├── README.md
└── src/platform_cli/
    ├── __init__.py
    ├── __main__.py         # argparse dispatch (mirror monitoring_agent/__main__.py main())
    ├── config.py           # read/write the DASHBOARD_AGENTS key in apps/dashboard/.env
    ├── compose.py          # up/down: subprocess `docker compose` (repo-root default)
    ├── registry_cmds.py    # agent add|list|remove
    ├── token_cmds.py       # token mint -> shells mint-admin-token.py
    └── doctor.py           # async httpx probe of /healthz /readyz per AgentSpec
```

It is a `uv` workspace member (the root globs `apps/*` already, per the root
`pyproject.toml` `[tool.uv.workspace] members`). The console-script entrypoint
`platform` is declared in its `pyproject.toml`, so after `uv sync` the operator
runs `platform …` directly — no `python -m` ceremony, no separate install.

```toml
# apps/platform-cli/pyproject.toml (sketch)
[project]
name = "platform-cli"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["dashboard", "httpx>=0.27"]   # reuse AgentConfig + the registry models

[project.scripts]
platform = "platform_cli.__main__:main"

[tool.uv.sources]
dashboard = { workspace = true }
```

Why a member with a console-script (not a `scripts/` file like
`mint-admin-token.py`): it needs to **import** the dashboard's `AgentConfig` /
`registry` to stay schema-consistent, which a loose `scripts/*.py` cannot do
cleanly; and a console-script gives a stable `platform <verb>` UX. We still
*reuse* `mint-admin-token.py` for `token mint` rather than reimplementing JWT.

## Command reference

| Command | What it does | Reads | Writes | Underlying seam |
| --- | --- | --- | --- | --- |
| `platform up [svc…] [--profile edge\|meta …]` | Bring up core infra + agents on `agent_backend`; `--profile` adds opt-in members | `docker-compose.yml`, `*/.env` | — (starts containers) | `docker compose up -d` (`+ --profile <p>` per flag), run from repo root |
| `platform down [svc…] [--profile edge\|meta …]` | Stop containers (keeps volumes) | `docker-compose.yml` | — | `docker compose down` (`+ --profile <p>` per flag), run from repo root |
| `platform agent add <slug> --url <u> [--kind server\|batch\|self] [--port N] [--title T] [--feature F …]` | Append/replace an `AgentConfig` in `DASHBOARD_AGENTS` | `apps/dashboard/.env` | `DASHBOARD_AGENTS` (that key only) | `dashboard.settings.AgentConfig` (validate) |
| `platform agent list [--json]` | Print the registered agents as the dashboard will see them | `apps/dashboard/.env` | — | `dashboard.registry.build_registry` |
| `platform agent remove <slug>` | Drop the entry with that slug | `apps/dashboard/.env` | `DASHBOARD_AGENTS` | `registry.by_slug` (existence check) |
| `platform token mint [--sub S] [--tenant T] [--role admin\|manager] [--ttl N] [--audience A]` | Mint an operator/ops login JWT | `$JWT_SECRET` (env) | — (prints token to stdout) | shells `apps/dashboard/scripts/mint-admin-token.py` |
| `platform doctor [--slug S] [--json]` | Probe `/healthz` + `/readyz` of each registered agent | `apps/dashboard/.env`, agents over HTTP | — | `registry` + httpx (mirrors `aggregator.py` probing) |

Notes consistent with ground truth:

- `--feature` values are validated against `registry.KNOWN_FEATURES`
  (`incidents, findings, runs, pm`). Per `registry.py:53` strings are
  lowercased/trimmed; the CLi does the same before write. An **unknown feature
  is rejected at the CLI** (fail-fast) even though the dashboard would silently
  ignore it — the CLI is the friendlier gate, the dashboard stays permissive.
- `--kind` is `Literal["server","batch","self"]`; a bad kind is rejected by
  `AgentConfig` validation (see `test_agent_config_rejects_unknown_kind`).
- `token mint` only allows roles the existing script allows (`admin`/`manager`);
  tenant defaults to `platform` (matching `mint-admin-token.py`). BR-002: tenant
  is a JWT claim minted by the host, never inferred elsewhere.
- After any `agent add/remove`, the CLI prints: *"registry changed — run
  `platform up dashboard` to apply"* (the registry is start-time only).
- `--profile` is a pass-through to the compose's named profiles
  (`edge` = Caddy TLS, `meta` = docker-socket-proxy).
  Bare `platform up` brings up **core only** (infra + the 3 agents); the gated
  members are inert until their profile is named (matches `docker-compose.yml`).

## Worked session

```bash
# 0. one-time: install the workspace (picks up apps/platform-cli)
uv sync

# 1. bring up core infra + the default agents (no profile = core only)
platform up
#   → docker compose up -d              (run from repo root; docker-compose.yml is the default)
#   → agent_backend network created if absent; postgres/redis/rabbitmq + agents start
#   (opt-in: `platform up --profile edge` adds Caddy, `--profile meta` docker-socket-proxy)

# 2. mint a login token to paste into the dashboard /login
export JWT_SECRET=$(cat /run/secrets/jwt_secret)   # host-held; never in repo (BR-010)
platform token mint --sub alice --role admin --ttl 3600
#   → eyJhbGciOiJIUzI1Ni␣...      (HS256, shared secret; same path as mint-admin-token.py)

# 3. attach a new agent I just deployed at http://invoices:8000
platform agent add invoices \
  --url http://invoices:8000 --kind server --port 8006 --feature runs
#   ✓ wrote DASHBOARD_AGENTS in apps/dashboard/.env (8 agents)
#   ! registry is read at dashboard start — run `platform up dashboard` to apply

# 4. apply it
platform up dashboard
#   → recreates the dashboard container; build_registry() now sees `invoices`

# 5. confirm what the dashboard will render
platform agent list
#   slug        kind    url                       features      port
#   monitoring  server  http://monitoring:8000    incidents     8002
#   …
#   invoices    server  http://invoices:8000      runs          8006

# 6. health-check the fleet (US-013 graceful: a missing agent is reported, not fatal)
platform doctor
#   dashboard   healthz ✓   readyz ✓
#   monitoring  healthz ✓   readyz ✓
#   invoices    healthz ✓   readyz ✗   (deps: postgres unreachable)
#   web-ext-pipeline  (batch, no port) — skipped, heartbeat-only

# 7. detach it again
platform agent remove invoices && platform up dashboard
```

## Registry-write contract

This is the rule that stops the CLI from becoming a second source of truth.

1. **The single source of truth is the `DASHBOARD_AGENTS` env value** consumed by
   `dashboard.settings.Settings.agents` (`validation_alias` =
   `AliasChoices("dashboard_agents", "agents")`). The CLI persists it in
   `apps/dashboard/.env` as a one-line JSON array. The CLI owns **only that one
   key**; it never touches other dashboard env (`JWT_SECRET`, `LLM_*`, …) and
   never writes a separate "registry file". There is no `platform-registry.json`.

2. **The shape is `AgentConfig`, imported — not re-declared.** Every write goes
   through `dashboard.settings.AgentConfig(**fields).model_dump()` so the JSON the
   CLI writes is *exactly* what the dashboard will parse. If `AgentConfig` gains a
   field, the CLI inherits it for free; if a value is invalid, `AgentConfig`
   raises before anything is written (no half-written `.env`). Round-trip check:
   the CLI re-parses its own output through `build_registry(Settings(...))` and
   asserts the slugs match before saving.

3. **Read path = the dashboard's read path.** `platform agent list` and
   `platform doctor` call `build_registry(get_settings())` against the same
   `apps/dashboard/.env`, so the CLI sees identical `AgentSpec`s
   (`slug/title/base_url/port/kind/has_*`) to what the dashboard renders. No
   parallel parser.

4. **Write algorithm** (idempotent, order-preserving):

   ```python
   # platform_cli/config.py  (sketch)
   from dashboard.settings import AgentConfig, Settings

   def load_agents(env_path) -> list[AgentConfig]:
       raw = read_dotenv_value(env_path, "DASHBOARD_AGENTS")
       if raw is None:                       # key absent -> seed from the live default
           return list(Settings().agents)    # DEFAULT_AGENTS, so add never loses the base set
       return [AgentConfig(**o) for o in json.loads(raw)]

   def upsert(agents, new: AgentConfig) -> list[AgentConfig]:
       out = [a for a in agents if a.slug != new.slug]   # replace-by-slug
       return out + [new]

   def save_agents(env_path, agents: list[AgentConfig]) -> None:
       payload = json.dumps([a.model_dump() for a in agents], separators=(",", ":"))
       # validate the payload through the SAME parser the dashboard uses
       build_registry(Settings(jwt_secret="x", agents=json.loads(payload)))
       write_dotenv_value(env_path, "DASHBOARD_AGENTS", payload)   # rewrites ONLY this key
   ```

5. **The CLI does not apply changes; it stages them.** A config write is inert
   until `platform up dashboard` recreates the process (registry is start-time
   only — `api.py:72`). The CLI says so on every mutation. This is deliberate:
   the running dashboard and the on-disk config can briefly differ, and the
   operator decides when to apply — there is no hidden live mutation path.

6. **Governance.** Tenant is never written into the registry — agents carry no
   tenant; tenant lives only in minted JWTs (BR-002). The CLI prints/reads no
   secrets into the registry (BR-010): `DASHBOARD_AGENTS` holds slugs/URLs/kinds
   only. `token mint` reads `JWT_SECRET` from the environment exactly like
   `mint-admin-token.py` and never writes it anywhere.

## Implementation plan

Dependency order; effort in person-hours.

1. **Scaffold member** — `apps/platform-cli/` package, `pyproject.toml` with the
   `platform` console-script + `dashboard`/`httpx` deps; add to CI matrix. (1h)
2. **`config.py`** — dotenv single-key read/write; `load_agents` /
   `upsert` / `save_agents` per the contract above; round-trip validation through
   `build_registry`. (2h)
3. **`registry_cmds.py`** — `agent add|list|remove` with `KNOWN_FEATURES` +
   `AgentConfig` validation and the "run `platform up dashboard`" notice. (2h)
4. **`compose.py`** — `up`/`down` shelling `docker compose` from repo root
   (`docker-compose.yml` is the default — no `-f`); `--profile` pass-through to
   the compose's `edge`/`meta` profiles; create `agent_backend`
   if absent; pass-through service names. (1.5h)
5. **`token_cmds.py`** — `token mint` shells
   `apps/dashboard/scripts/mint-admin-token.py` (subprocess, same flags), so the
   JWT path stays in one place. (1h)
6. **`doctor.py`** — async httpx GET `/healthz` + `/readyz` per `AgentSpec`,
   skipping `kind=="batch"`/`port==0` (mirror `aggregator.py:196-200`); table +
   `--json`. (2h)
7. **`__main__.py`** — argparse subcommands; `main()` mirrors
   `monitoring_agent/__main__.py`. (1h)
8. **Tests** — see below. (3h)
9. **Docs** — in `docs/add-your-own-agent.md` §5 (*Wire it up*), replace the
   manual **dashboard-registration** step (currently "Register it in
   `apps/dashboard` settings (`AGENT_<NAME>_URL`)") with `platform agent add
   <slug> --url … [--feature …]`. The other §5 steps stay manual: the
   `infra/bootstrap.sql` DB add (CLI only *reminds*), the `env.example`, and the
   CI-matrix entry. Cross-link from `docs/deploy-your-own.md`. (1h)

**Total: ~14.5 person-hours** for v0.1.

The compose file referenced by `up`/`down` is the repo-root `docker-compose.yml`
— the **default** compose, so the CLI shells plain `docker compose up -d` /
`docker compose down` from the repo root (no `-f`). It aggregates the per-member
composes, creates `agent_backend`, and gates edge-Caddy/docker-socket-proxy
behind the `edge`/`meta` profiles. `compose.py` keeps the repo-root
path in one constant (and the working directory it runs from); if the layout ever
changes, only that constant moves.

## Test plan

```bash
uv run pytest apps/platform-cli/tests -q
```

- `test_config_roundtrip.py` — write N agents, re-parse via `build_registry`,
  assert slugs/kinds/features match the dashboard's view. `@spec US-013`.
- `test_upsert_replaces_by_slug.py` — adding an existing slug replaces (not
  duplicates); order preserved; remove drops exactly one.
- `test_seed_from_default.py` — when `DASHBOARD_AGENTS` is absent, `add` seeds
  from `DEFAULT_AGENTS` so the base set is not silently dropped.
- `test_rejects_unknown_feature.py` — `--feature bogus` is rejected at the CLI;
  `--kind bogus` is rejected by `AgentConfig` (parity with
  `test_agent_config_rejects_unknown_kind`).
- `test_only_dashboard_agents_key_written.py` — other `.env` keys
  (`JWT_SECRET`, `LLM_*`) are byte-for-byte unchanged after a write (no second
  source of truth, BR-010). 
- `test_token_mint_shells_script.py` — `token mint` invokes
  `mint-admin-token.py` with the right flags; with no `JWT_SECRET` it exits
  non-zero and prints nothing to stdout (NS-002 / BR-010).
- `test_doctor_skips_batch.py` — a `kind=batch`/`port=0` agent is not probed;
  an unreachable agent is reported, not raised (US-013 graceful degradation).

Manual: full `platform up → token mint → agent add → up dashboard → list →
doctor → remove` against a local compose, confirming the new agent's panel
appears/disappears in the dashboard after each `up dashboard`.

## Risk

- **Config/runtime skew.** The on-disk `DASHBOARD_AGENTS` can differ from the
  running dashboard until `up dashboard`. Mitigation: every mutation prints the
  apply step; `platform doctor` reads the same on-disk config the operator just
  edited, so "did it apply?" is one command away. Accepted by design (registry is
  start-time only; a live `/reload` is an explicit non-goal here).
- **`.env` rewrite corruption.** A naive rewrite could clobber other keys.
  Mitigation: single-key read/modify/write with an atomic temp-file rename;
  `test_only_dashboard_agents_key_written` gates it. The CLI never reformats
  unrelated lines.
- **Schema drift from the dashboard.** If the CLI ever copied the `AgentConfig`
  shape it would rot. Mitigation: it **imports** `AgentConfig`/`build_registry`
  (workspace dep on `dashboard`); a CI grep can assert `platform_cli` defines no
  local `class AgentConfig`.
- **Secret leakage (BR-010).** `token mint` handles `JWT_SECRET`. Mitigation:
  read from env only (never an arg, never written to `.env` by the CLI); reuse
  `mint-admin-token.py` which already enforces this. Tenant stays a JWT claim
  (BR-002), never a registry field.
- **Privilege boundary.** `up`/`down` and DB bootstrap may need elevated rights
  the operator holds, not the CLI. Mitigation: the CLI shells `docker compose`
  (operator's docker group) and *prints* the `bootstrap.sql` command rather than
  running privileged SQL itself — it never assumes passwordless sudo.
- **Lock-in regression (ADR-0001).** `up`/`down` must stay plain
  `docker compose`; never reach for a managed/LangGraph-Platform runner.
  Mitigation: `compose.py` shells the OSS docker compose only; no SDK.

## ADR candidate

This introduces an operator interface and a config-write contract — worth an ADR
(next free id **ADR-0009**): "Operator `platform` CLI owns the `DASHBOARD_AGENTS`
config key as the single registry source of truth; no live registry mutation."
See §Registry-write contract for the decision and trade-off.
