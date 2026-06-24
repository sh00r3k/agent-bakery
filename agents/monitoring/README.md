# monitoring-agent

Watches your public-facing services and turns raw signals into **triaged,
deduplicated alerts** for operators. A standalone agent built on
[`agentkit`](../../packages/agentkit) (shared config / LLM / DB / RabbitMQ
contract). Embeds the OSS LangGraph library in-process — no LangGraph Server.

```
signals ──► LangGraph triage ──► RabbitMQ (agent.alerts) ──► chat microservice
            ingest→classify→dedup→decide→notify
            persisted in Postgres `incidents`
```

## Signals / intake

| Source        | How                                  | Notes                                           |
| ------------- | ------------------------------------ | ----------------------------------------------- |
| Sentry        | `POST /webhook/sentry`               | Issue-alert payload → title/culprit/level/count |
| Health sweep  | APScheduler, `poll_interval_seconds` | Probes `targets`: status, latency, TLS expiry   |
| Generic alert | `POST /webhook/alert`                | Alertmanager-style JSON (one or many alerts)    |

Each source is normalized into a provider-agnostic `Signal` (in `collectors.py`)
carrying a stable `fingerprint`, so downstream triage never touches raw vendor
JSON.

## Triage graph (`graph.py`)

`ingest → classify → dedup → decide → notify`

- **classify** — LLM (via `agentkit.LLMClient`, OpenAI-compatible gateway)
  returns `{severity: info|warning|critical, category, hypothesis}`. Falls back
  to the source-suggested severity if the LLM is unavailable or returns junk.
- **dedup** — upsert into `incidents` keyed by `sha256(source + fingerprint)`.
  A repeat seen within `dedup_window_minutes` bumps `count` and is **suppressed**
  (no re-alert) — except `critical`, which always re-pages.
- **decide** — routes alert vs suppress from severity + dedup state.
- **notify** — publishes an `agentkit.Alert` to RabbitMQ (`agent.alerts`
  topic), which the forwarder bridges to a chat microservice. Never calls a
  chat-platform API directly (BR-009 / AR-5).

Every incident is persisted regardless of whether it alerts.

## Persistence (`store.py`)

`incidents (id, dedup_key, source, severity, title, body, count, first_seen,
last_seen, status)` — created idempotently on startup (`CREATE TABLE IF NOT
EXISTS`). Raw parameterized SQL via psycopg3 (`%s` placeholders), pool from
`agentkit.db.pg_pool` (autocommit).

## API

| Method | Path              | Auth        | Purpose                           |
| ------ | ----------------- | ----------- | --------------------------------- |
| POST   | `/webhook/sentry` | open\*      | Sentry intake                     |
| POST   | `/webhook/alert`  | open\*      | Generic alert intake              |
| GET    | `/incidents`      | admin (JWT) | Recent triaged incidents          |
| POST   | `/sweep`          | admin (JWT) | Trigger a manual health sweep     |
| GET    | `/healthz`        | open        | Liveness (inherited)              |
| GET    | `/readyz`         | open        | Readiness: PG + Redis (inherited) |

\* Webhooks are unauthenticated by design (Sentry/Alertmanager post to them);
put them behind the reverse proxy / a shared secret path in production. Admin
endpoints require an HS256 bearer token minted by the host (`require_admin`).

## Configuration

Agent-specific settings (full infra contract in `env.sample`):

| Env                      | Default                                             |
| ------------------------ | --------------------------------------------------- |
| `POLL_INTERVAL_SECONDS`  | `60`                                                |
| `TARGETS` (JSON list)    | `["https://example.com","https://app.example.com"]` |
| `SLOW_THRESHOLD_SECONDS` | `2.0`                                               |
| `CERT_WARN_DAYS`         | `14`                                                |
| `PROBE_TIMEOUT_SECONDS`  | `10.0`                                              |
| `DEDUP_WINDOW_MINUTES`   | `30`                                                |
| `REDIS_DB`               | `2`                                                 |
| `PORT`                   | `8000` (host binds `127.0.0.1:8002`)                |

## Run

```bash
# local dev
pip install -e .[dev]
cp env.sample .env   # fill secrets; never commit .env
python -m monitoring_agent      # serves on $HOST:$PORT

# container (joins the external agent_backend network)
docker compose up -d --build         # host: 127.0.0.1:8002 -> 8000
```

## Tests

```bash
pip install -e .[dev]
pytest -q
```

Tests use fakes (in-memory store, fake LLM, monkeypatched `publish_alert`) — no
live Postgres / RabbitMQ / LLM gateway required:

- `test_collectors.py` — Sentry + Alertmanager parsing, probe→signal thresholds.
- `test_store.py` — dedup-key stability + upsert count/window semantics.
- `test_graph.py` — full triage: classify, suppress within window, critical
  re-alert, LLM-failure fallback.
