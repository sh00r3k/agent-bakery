# hello-agent

The smallest possible agent built on `agentkit`: env-driven settings, a 1-node
LangGraph, and the shared FastAPI factory — about 40 lines in
[`hello_agent.py`](./hello_agent.py).

## Run

```bash
uv sync                      # from the repo root
cd examples/hello-agent
uv run --project ../.. uvicorn hello_agent:app --reload
```

Then:

```bash
curl -X POST localhost:8000/echo \
  -H 'content-type: application/json' \
  -d '{"text": "world"}'
# {"text":"world","reply":"hello, world!"}

curl localhost:8000/healthz   # {"status":"ok"}
```

> Without Redis, the rate-limit middleware logs `server.ratelimit_redis_unavailable`
> and the request still succeeds (with a short delay). To get instant responses,
> run `docker compose up -d redis` from the repo root first.

## What it demonstrates

1. `class Settings(BaseAgentSettings)` — config from env (`GREETING=hi` overrides
   the default greeting).
2. `StateGraph` with a single node compiled to `GRAPH`.
3. `create_app(settings, title=...)` — gives you `/healthz`, `/readyz`,
   `/metrics.json`, structured logging, and error handling for free.

Copy this file as the starting point for a real agent — see
[`docs/add-your-own-agent.md`](../../docs/add-your-own-agent.md).
