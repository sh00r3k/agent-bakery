# Deploy your own agents

Everything is env-driven. No business config is baked in — you bring your own
host, domain, and LLM gateway.

## 1. Bring up the shared infra

Your agents expect a docker network `agent_backend` with Postgres (pgvector),
Redis, and RabbitMQ. If you already run them, just attach. Otherwise stand up a
minimal stack and create the network:

```bash
docker network create agent_backend
# bring up postgres (pgvector), redis, rabbitmq joined to agent_backend
```

Create one database per agent (idempotent):

```bash
docker exec -i agent-postgres-1 psql -U "$POSTGRES_USER" -d postgres \
  < infra/bootstrap.sql
```

`infra/bootstrap.sql` creates one DB per agent and enables the `vector`
extension in each. The members this repo ships are `monitoring` and `dashboard`.
Edit the script to match the agents you actually run.

## 2. Configure each agent

Every member has an `env.example`. Copy it to `.env` and fill in:

```bash
cp agents/monitoring/env.example agents/monitoring/.env
$EDITOR agents/monitoring/.env   # set LLM_BASE_URL, LLM_API_KEY, JWT_SECRET, ...
cp apps/dashboard/env.example   apps/dashboard/.env
$EDITOR apps/dashboard/.env
```

Key knobs (see `agentkit.config.BaseAgentSettings`):

| Var | Meaning |
|-----|---------|
| `LLM_BASE_URL` | Your OpenAI-compatible gateway, e.g. `https://your-gateway.example.com/v1` (LiteLLM / vLLM / Ollama / OpenAI). |
| `LLM_API_KEY` | Gateway key (leave empty for keyless local gateways). |
| `JWT_SECRET` | HS256 secret the host signs tokens with; agents verify against it. |
| `POSTGRES_*` / `REDIS_*` / `RABBITMQ_URL` | Shared infra connection. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Empty by default (tracing off); see below. |

## 3. Build & run

Build with the **repo root as context** so the workspace `agentkit` is available:

```bash
docker build -f agents/monitoring/Dockerfile -t monitoring .
docker build -f apps/dashboard/Dockerfile    -t dashboard  .
```

Each member listens on `:8000` in-container; map distinct host loopback ports
(e.g. monitoring `8002`, dashboard `8005`) and join `agent_backend`. (Building
from source is for contributors/forks; most operators pull published images —
see **Release & published images** below.)

## 3a. Release & published images

Most operators do not build from source — they pull pre-built images. The release
flow:

1. A maintainer pushes a `v*.*.*` git tag (e.g. `git tag v0.2.0 && git push --tags`).
2. `.github/workflows/release.yml` builds the `monitoring` and `dashboard` images
   from their Dockerfiles (repo-root context) and pushes them to GHCR under
   `ghcr.io/<owner>/agent-bakery`, tagged with both the version and `latest`.
3. Operators point `REGISTRY` / `IMAGE_TAG` at that release and pull:

   ```bash
   docker compose pull        # fetch ${REGISTRY}/{monitoring,dashboard}:${IMAGE_TAG}
   docker compose up -d
   ```

Building from source (§3) is the contributor/fork path: uncomment the `build:`
stanza per service in `docker-compose.yml` instead of pulling. Pin a real
`IMAGE_TAG` in prod (the root `env.example` ships `v0.2.0`); never run `latest`.

## 4. Edge / TLS

`infra/edge/` ships a Caddy reference (`Caddyfile` + `docker-compose.yml`) that
auto-provisions Let's Encrypt TLS and routes subdomains/paths to each agent's
loopback port. Point your DNS at the host and edit the domain in the `Caddyfile`.

## 5. Optional extras

- **Observability** — tracing is **off by default**; your agents run fine
  without it. To export OpenTelemetry traces, install agentkit's `observability`
  extra and set `OTEL_EXPORTER_OTLP_ENDPOINT` to your OTLP collector per agent.
  A missing/incompatible OTel SDK degrades to structured logs only, so a bad dep
  never breaks a run.
- **Container introspection** — the monitoring agent can read container state
  via `infra/docker-socket-proxy.yml` (a read-only Docker API proxy; never the
  raw socket).
- **Operator CLI** — `apps/platform-cli` ships a `platform` console script that
  owns the `DASHBOARD_AGENTS` registry key and drives the compose lifecycle, token
  minting, and per-agent health probes. Use `platform agent add <slug> --url …
  [--feature …]` to attach an already-deployed agent to the dashboard (see
  [add-your-own-agent.md](add-your-own-agent.md) §5).
- **Hardening** — `infra/nginx-hardening.conf`, `infra/ollama-tuning.md`.
