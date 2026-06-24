# Design — Docker/compose distribution

**Date:** 2026-06-20
**Status:** Implemented (root compose, `env.example`, release CI; per-member docs migration pending)
**Refs:** `docker-compose.yml` (root), `env.example` (root), `docs/deploy-your-own.md`, `docs/architecture.md` (Layers, Versioning policy), `infra/bootstrap.sql`, `infra/env.example`, `.github/workflows/release.yml`, `agents/monitoring/Dockerfile`, `agents/monitoring/docker-compose.yml`, `agents/monitoring/env.example`, `apps/dashboard/Dockerfile`, `apps/dashboard/docker-compose.yml`, `apps/dashboard/env.example`, `infra/edge/docker-compose.yml`, `infra/edge/Caddyfile`, `infra/docker-socket-proxy.yml`, `apps/dashboard/scripts/mint-admin-token.py` · ADR-0001, NS-002, NS-003, BR-002, BR-010

## Why

`docs/deploy-your-own.md` today is a **manual checklist**: create the network by
hand, run `bootstrap.sql`, copy each `env.example`, then `docker build -f …` and
`docker run` every member one at a time. That is fine for the maintainer who knows
the wiring; it is a wall for an adopter who just wants the platform up. The gotchas
are load-bearing and easy to get wrong: the network must exist *first* (every
member declares it `external`), container names must be exactly `agent-postgres-1`
/ `agent-redis-1` / `agent-rabbitmq-1` (bootstrap + env references depend on them),
both agent images need the **repo root** as build context, and `JWT_SECRET` must
match across all agents and the host that mints tokens.

This design turns that into **one command**: a root `docker-compose.yml` that owns
the `agent_backend` network, brings up shared infra + the agents, runs
`bootstrap.sql` on first boot, and gates the optional pieces (Caddy,
docker-socket-proxy) behind compose **profiles**. It composes the existing
per-member files — same service names, ports, contexts, `env_file` paths — it does
not replace them.

## Scope

In:
- Root `docker-compose.yml`: infra (`postgres`/`redis`/`rabbitmq`) + the two
  members this repo ships (`monitoring_agent`, `dashboard`) on `agent_backend`.
- Registry-published-image vs build-from-source toggle per service.
- Versioning/tag policy tied to `docs/architecture.md`.
- `bootstrap.sql` wired as a first-boot init step.
- Optional `edge` / `meta` profiles.
- Root `env.example` for compose-interpolated knobs only (no inlined secrets).
- Migration notes from `docs/deploy-your-own.md`.

Out (NS-002, ADR-0001):
- No identity store ships in the distribution. The host owns identity; the dist
  only verifies HS256 JWTs. First-token minting is documented, not bundled.
- No LangGraph Platform/Server/Studio (NS-003, ADR-0001). LangGraph stays the
  in-process OSS library inside each agent image; cross-agent calls are plain
  HTTP over `agent_backend`.
- Ollama and the OpenAI-compatible LLM gateway are **not** containerized — they
  remain on the host/external, reached via `host.docker.internal`.

## Layer placement

Per `docs/architecture.md` §Layers, the distribution is a **Layer 1 concern**
(shared infra + the `agent_backend` network) plus the *packaging* of Layer 3
members. It introduces no new layer and no new `agentkit` surface: the root
compose only references existing Dockerfiles, env files, and `bootstrap.sql`. The
dependency rule is preserved — infra has no upward dependency; agents
`depends_on` infra healthchecks, never each other for startup.

## Contract

### 1. Registry-published images vs build-from-source

Default is **pull a published image**; build-from-source is an opt-in override.

```yaml
monitoring_agent:
  image: ${REGISTRY:-ghcr.io/acme/agent-bakery}/monitoring:${IMAGE_TAG:-latest}
  # build:
  #   context: .
  #   dockerfile: agents/monitoring/Dockerfile
```

- **Published image** (operators): `docker compose pull && docker compose up -d`.
  No build toolchain, no repo checkout of `packages/agentkit`, fast.
- **Build from source** (contributors / forks): uncomment `build:` (or use a
  `docker-compose.build.yml` override) and `docker compose build`. The context is
  the **repo root** (`context: .`) exactly as `agents/monitoring/Dockerfile` and
  `apps/dashboard/Dockerfile` require — both `COPY pyproject.toml uv.lock`,
  `packages/agentkit`, and their own member dir, then `uv sync --frozen`.

Images are built by the *same* Dockerfiles used today (digest-pinned `uv` +
`python:3.12-slim`, non-root uid 10001, curl-only runtime, `/healthz`
healthcheck). The published image is just a CI build of that Dockerfile pushed to
`${REGISTRY}`. `${REGISTRY}` and `${IMAGE_TAG}` come from the root `.env`; no
registry literal is baked in (the default is an Acme placeholder, BR-010). This
distribution ships exactly two members — `monitoring` and `dashboard` — so those
are the only `${REGISTRY}/<svc>` images this repo's release CI builds and
publishes.

### 2. Versioning / tags

Tags follow `docs/architecture.md` §Versioning policy verbatim:

| Change                                                                           | Bump   | Image tag effect |
| -------------------------------------------------------------------------------- | ------ | ---------------- |
| Bug fix, no contract change                                                      | Patch  | `v0.2.2 → v0.2.3` |
| New optional field / new agentkit export / new agent                             | Minor  | `v0.2.x → v0.3.0` |
| Removed/renamed agentkit export, changed agent HTTP contract, changed event name | Major  | `v0.x → v1.0.0`   |
| Changed default AI/tenancy/cost behavior                                         | Major (+ ADR) | as above   |

Rules:
- **One tag for the whole platform.** All images share `${IMAGE_TAG}` so a
  released set is mutually compatible — the agentkit contract (`auth`, `db`,
  `server`) is shared, and CR-3/AR-2 make cross-agent
  contracts additive only within a minor line.
- **Pin in prod, never `latest`.** The root `env.example` ships
  `IMAGE_TAG=v0.2.0`; `latest` is only the unset fallback for local poking.
- A **Major** bump may carry a `bootstrap.sql` migration (see §3) and an ADR.

### 3. The `bootstrap.sql` init step

`infra/bootstrap.sql` is mounted into Postgres's first-boot init dir:

```yaml
postgres:
  image: pgvector/pgvector:pg16
  container_name: agent-postgres-1
  volumes:
    - pgdata:/var/lib/postgresql/data
    - ./infra/bootstrap.sql:/docker-entrypoint-initdb.d/bootstrap.sql:ro
```

On an **empty data dir**, the entrypoint runs `bootstrap.sql` via `psql` (so the
`\connect` / `\gexec` meta-commands work). It is idempotent — `CREATE DATABASE …
WHERE NOT EXISTS \gexec`, `CREATE EXTENSION IF NOT EXISTS vector`. It creates the
per-agent DBs this distribution ships — `monitoring` and `dashboard` — each with
the pgvector extension. (Per-agent table schemas are owned/migrated by each agent
itself, not by `bootstrap.sql`.)

Operational notes:
- The init dir only runs on a **fresh volume**. After adding an agent later,
  re-run manually (the script is idempotent):
  `docker exec -i agent-postgres-1 psql -U appuser -d postgres < infra/bootstrap.sql`.

### 4. The network

The root compose **owns** `agent_backend` and creates it:

```yaml
networks:
  agent_backend:
    name: agent_backend     # NOT external here — this file creates it
```

Every per-member file keeps `external: true, name: agent_backend` and attaches to
it. This resolves the "network must exist first" gotcha: bringing up the root
compose creates it; bringing up a single member afterward (e.g. `cd
agents/monitoring && docker compose up`) attaches to the already-created network.
`dashboard` keeps its network **alias** `dashboard` so the registry/self URL
resolves regardless of `container_name`.

### 5. Optional profiles

| Profile | Service               | What it adds                                   |
| ------- | --------------------- | ---------------------------------------------- |
| `edge`  | `edge-caddy`          | TLS + routing. `network_mode: host`; reaches agents via `127.0.0.1:800X` loopback (NOT container names). Mounts `infra/edge/Caddyfile`. |
| `meta`  | `docker-socket-proxy` | Read-only Docker API proxy for meta-monitoring at `http://docker-socket-proxy:2375`. Single source of truth (the standalone `infra/docker-socket-proxy.yml` is the same service — run ONLY one). |

```bash
docker compose up -d                  # core: infra + monitoring + dashboard
docker compose --profile edge up -d   # + Caddy
docker compose --profile meta up -d   # + docker-socket-proxy
```

> **Naming caveat:** `agents/monitoring/docker-compose.yml` names the proxy
> `docker-socket-proxy`; `infra/docker-socket-proxy.yml` names it `docker-proxy`.
> The root compose uses `docker-socket-proxy` (matching the monitoring env
> default `DOCKER_PROXY_URL=http://docker-socket-proxy:2375`). If you run the
> standalone infra file instead, point `DOCKER_PROXY_URL` at `docker-proxy:2375`.

### 6. Secrets & first-token minting (NS-002 / BR-010)

The dist ships **no identity store** (NS-002): agents only *verify* HS256 JWTs
into a `Principal(sub, tenant, role)`; they run no login. The root `env.example`
carries only compose-interpolated knobs (`REGISTRY`, `IMAGE_TAG`,
`POSTGRES_USER/PASSWORD`, RabbitMQ creds) — **no app secrets** (BR-010).
`POSTGRES_PASSWORD` ships empty + required (compose fails fast if unset).
`RABBITMQ_USER/PASS` default to `guest/guest` for a loopback-only local stack;
the file flags that this weak default MUST be changed before any non-loopback
exposure (BR-010 hygiene). App
secrets (`JWT_SECRET`, `LLM_API_KEY`, `WEBHOOK_SECRET`) live in
per-member `.env` (`agents/<name>/.env`, `apps/dashboard/.env`), referenced via
`env_file:` and gitignored.

`JWT_SECRET` MUST be identical across all agents and the host that mints tokens.
The operator mints the **first ops/dev JWT** host-side with
`apps/dashboard/scripts/mint-admin-token.py` — the documented signing path, not a
login DB:

```bash
# bootstrap the first operator login (paste once into the dashboard /login form)
JWT_SECRET=$(grep '^JWT_SECRET=' apps/dashboard/.env | cut -d= -f2-) \
  python apps/dashboard/scripts/mint-admin-token.py --sub op --tenant platform --role admin --ttl 3600
```

The script signs `{sub, tenant, role, exp}` with `JWT_SECRET` (HS256), optional
`aud` via `--audience`. `verify_token` in `agentkit.auth` requires explicit
`tenant` + `role` claims (never falls back to `iss`) — `mint-admin-token.py`
always sets both. `--tenant platform --role admin` mints the cross-tenant `ops`
console identity (the documented BR-002 exception); a dev would mint
`--tenant acme --role manager` for a single-tenant token.

## Implementation plan

| # | Step | Effort |
| - | ---- | ------ |
| 1 | Root `docker-compose.yml` — infra + monitoring + dashboard + 3 profiles, image/build toggle. **Done.** | 2h |
| 2 | Root `env.example` — REGISTRY/IMAGE_TAG/POSTGRES/RABBITMQ only. **Done.** | 0.5h |
| 3 | `monitoring` + `dashboard` Dockerfiles (repo-root context, digest-pinned bases, non-root uid 10001, `/healthz`). **Done** — `agents/monitoring/Dockerfile`, `apps/dashboard/Dockerfile` both exist. | — |
| 4 | Release CI (`.github/workflows/release.yml`): on a pushed `v*.*.*` git tag, build+push `${REGISTRY}/{monitoring,dashboard}:${tag}` and `:latest` to GHCR (repo-root context, `docker/metadata-action` tags). **Done** (this change). | 3h |
| 5 | Optional `docker-compose.build.yml` override so contributors `docker compose -f docker-compose.yml -f docker-compose.build.yml build`. | 1h |
| 6 | Rewrite `docs/deploy-your-own.md` §§1–5 to the one-command flow (keep the manual path as an appendix). | 1.5h |
| 7 | Document the `mint-admin-token.py` first-token step in the deploy doc. | 0.5h |

**Total: ~9 person-hours** (steps 1–4 landed; steps 5–7 — the deploy-doc
migration — remain).

## Test plan

```bash
# 1. Config validates for core + all profiles
docker compose config -q
docker compose --profile edge --profile meta config -q

# 2. Cold start brings up infra + agents healthy
cp env.example .env && $EDITOR .env            # POSTGRES_PASSWORD, IMAGE_TAG
docker compose up -d
docker compose ps                              # postgres/redis/rabbitmq + monitoring + dashboard = healthy

# 3. bootstrap.sql ran on first boot — every agent DB exists with pgvector
docker exec agent-postgres-1 psql -U appuser -d postgres -c "\l" | grep -E 'monitoring|dashboard'
docker exec agent-postgres-1 psql -U appuser -d dashboard -c "\dx" | grep vector

# 4. Agent health endpoints (loopback bind) — only the two agents that ship
curl -fsS http://127.0.0.1:8005/healthz        # dashboard
curl -fsS http://127.0.0.1:8002/healthz        # monitoring

# 5. First-token mint + authed read (BR-002: tenant from JWT, never query)
TOK=$(JWT_SECRET=$(grep ^JWT_SECRET= apps/dashboard/.env|cut -d= -f2-) \
  python apps/dashboard/scripts/mint-admin-token.py --sub op --role admin)
curl -fsS -H "Authorization: Bearer $TOK" http://127.0.0.1:8005/   # dashboard renders

# 6. Profiles
docker compose --profile edge up -d            # Caddy TLS on host 80/443
docker compose --profile meta up -d            # docker-socket-proxy reachable from monitoring
```

Assertions:
- `@spec BR-010` — no secret/key/host-IP in `env.example` or `docker-compose.yml`
  (gitleaks in CI). Only placeholder `REGISTRY`, empty `POSTGRES_PASSWORD`.
- `@spec NS-002` — grep the compose for any identity/login service → zero rows.
- `@spec ADR-0001` — no LangGraph Platform/Studio image, no `langgraph-cli`
  service; agents run the in-process library.

## Risk

- **`latest` in prod.** `IMAGE_TAG` defaults to `latest` if unset → drift.
  Mitigation: `env.example` ships a pinned `v0.2.0`; CI/release docs forbid
  `latest`.
- **First-boot-only init.** `bootstrap.sql` runs only on an empty `pgdata`
  volume. Adding an agent later silently skips DB creation. Mitigation: documented
  idempotent re-run; the script is safe to apply any time.
- **Proxy service-name mismatch.** `docker-socket-proxy` (root/monitoring) vs
  `docker-proxy` (standalone infra) — wrong `DOCKER_PROXY_URL` blinds
  meta-monitoring. Mitigation: §5 caveat; root compose standardizes on
  `docker-socket-proxy` matching the monitoring env default.
- **`JWT_SECRET` drift.** If a member's `.env` has a different secret, its tokens
  fail `verify_token` (401). Mitigation: deploy doc states the one-secret rule;
  consider a future single root secret fanned out via env interpolation.
- **Caddy host networking.** `edge-caddy` binds host 80/443 and can only reach
  agents via `127.0.0.1:800X`, not container names — a published-port change
  breaks routing silently. Mitigation: the `edge` profile and the Caddyfile both
  document loopback-only reachability.
