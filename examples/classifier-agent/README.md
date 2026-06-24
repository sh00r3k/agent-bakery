# classifier-agent

A reference agent that classifies a single text into a closed label set —
demonstrating the **prompt-safe, schema-validated LLM I/O** pattern every
agent-bakery agent should follow. Where `hello-agent` shows the bare
`agentkit` + LangGraph skeleton, this one shows the *LLM* parts done right.

## Run

```bash
uv sync                          # from the repo root
cd examples/classifier-agent
uv run --project ../.. uvicorn classifier_agent.api:app --reload
```

Then:

```bash
curl -X POST localhost:8000/classify \
  -H 'content-type: application/json' \
  -d '{"text": "the app crashes every time I hit save"}'
# {"label":"bug","confidence":0.93,"rationale":"reports a reproducible crash","fell_back":false}

curl localhost:8000/healthz      # {"status":"ok","agent":"classifier-agent"}
```

Point `LLM_BASE_URL` / `LLM_API_KEY` at your OpenAI-compatible gateway (LiteLLM /
vLLM / Ollama / OpenAI). With no gateway reachable the call fails and the agent
returns the deterministic fallback (`label: "other"`, `fell_back: true`) rather
than erroring — see below.

## What it demonstrates

The whole point lives in [`classifier_agent/graph.py`](./classifier_agent/graph.py):

1. **Fence the untrusted input.** `agentkit.fence_untrusted(text, max_chars=…)`
   truncates the caller's text (AF-08 token blow-up) and wraps it in
   `<<UNTRUSTED_SIGNAL>>` / `<<END_UNTRUSTED_SIGNAL>>` markers. The system prompt
   declares that block is *data to classify, never instructions* (AF-01 indirect
   prompt injection).
2. **Get structured output, not a string.**
   `llm.complete_json(messages, schema=Classification)` returns a validated
   [`Classification`](./classifier_agent/schema.py) pydantic model. On a non-JSON
   or schema-invalid reply it re-asks the model with a corrective message
   (bounded retry) before giving up — all under the same per-request USD cost
   ceiling and circuit breaker as `complete()`.
3. **Always have a deterministic fallback.** The graph node catches *every*
   failure (cost ceiling, circuit open, exhausted retries, transport error) and
   any off-set label, and returns `fallback_label` with `fell_back=True`. The
   agent never 500s because the model misbehaved.

`create_app(settings, …)` supplies `/healthz`, `/readyz`, `/metrics.json`,
structured logging, rate limiting and error handling for free.

## The pattern vs. the monitoring agent

`agents/monitoring` predates the shared helpers and hand-rolled its JSON parser
and fencing inline. This example consumes the `agentkit` primitives
(`fence_untrusted`, `SIGNAL_OPEN`/`SIGNAL_CLOSE`, `LLMClient.complete_json`)
directly — copy *this* file when starting a new LLM agent.

## Tests

```bash
uv run pytest examples/classifier-agent/tests -q   # from the repo root
```

The tests use a `FakeLLMClient` (no network) to exercise the happy path, the
off-set-label and failure fallbacks, and the input-fencing/truncation guards.

## Promote to a real agent

To run this in production, move the directory under `agents/classifier`, then per
[`docs/agent-standard.md`](../../docs/agent-standard.md): re-add
`[tool.uv.sources] agentkit = { workspace = true }`, add an `env.example` +
`Dockerfile`, register it on the dashboard (`platform agent add classifier …`),
and add it to the CI matrix. (It is stateless, so it needs no Postgres database
or `infra/bootstrap.sql` entry.)
