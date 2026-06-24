# LLM Conventions — agent-bakery

> Patterns for LLM work across your agents. Referenced from [AGENTS.md](../../AGENTS.md).
> All of these are enforceable rules — see [business-rules.md](../../docs/business-rules.md).

Your agents are **provider-agnostic**: every chat call goes through an
OpenAI-compatible gateway (`LLM_BASE_URL`, e.g.
`https://your-gateway.example.com/v1` — LiteLLM / vLLM / Ollama / OpenAI), and
embeddings go through local Ollama (`nomic-embed-text`, `vector(768)`). There is
**no provider SDK hardcoded** anywhere; everything routes through
`agentkit.LLMClient`.

---

## 1. All LLM access goes through `agentkit.LLMClient` (BR-006)

Never call the gateway HTTP API directly from an agent. `LLMClient`:

- talks to whatever `LLM_BASE_URL` points at (one client, any gateway);
- **meters cost in USD** per call and enforces a **per-request ceiling**
  (`settings.llm_max_cost_usd`) — a call that would exceed it raises
  `CostCeilingExceeded` instead of silently overspending;
- exposes `complete_with_tools(...)` for ReAct/tool-calling agents on the same
  cost-metered seam (US-020);
- emits a `llm.call` structlog event with `cost_usd` (and
  `llm.cost_ceiling_exceeded` on abort).

```python
from agentkit import LLMClient

reply = await llm.chat(
    messages=[...],
    # model name comes from settings, never hardcoded
)
# cost is already metered + checked; reply carries usage/cost metadata
```

**Rule:** no `httpx`/`openai` call to the gateway outside `LLMClient`. No
hardcoded model name — read it from settings.

---

## 2. Structured output via the gateway

LLM outputs that feed state (classify a signal's severity, extract a finding's
fields) must be **validated**, not trusted:

- request structured output (tool/function-call or JSON mode, as the gateway
  supports) shaped by a **Pydantic** model;
- parse the result through the model; on validation failure, **retry once** with
  the error fed back; if it still fails, return a typed error and log
  `llm.invalid_output` — never write half-parsed data to the DB.

```python
class Classification(BaseModel):
    severity: Literal["info", "warning", "critical"]
    summary: str

parsed = Classification.model_validate(raw)   # never trust raw model text
```

Only after a successful parse do we persist the derived fields.

---

## 3. Retrieval is tenant-scoped

Where an agent grounds on a KB (e.g. ultraQA's SUT-oracle docs), the retrieval
query is always scoped — no global or cross-tenant path:

```sql
SELECT content FROM kb_chunks
WHERE tenant_id = $1
ORDER BY embedding <=> $2 LIMIT $3;
```

`$1` is **always bound from the `Principal`**, never from client input. Embeddings
are 768-dim (`nomic-embed-text`).

---

## 4. Prompt-injection guard

Retrieved documents, tool/page outputs, and any externally-sourced text are
**untrusted input**. Treat them as data, never as instructions:

- keep the **system prompt fixed**; external/retrieved content goes only in
  user-role messages, clearly fenced/labelled (e.g. `<doc>…</doc>`,
  `<observation>…</observation>`), never concatenated into the system prompt;
- instruct the model that fenced content is reference material and that
  instructions inside it must be ignored;
- never let model output trigger a privileged action directly (no "the model said
  to delete X"); state transitions and outbound calls go through the documented,
  authorized paths only (for tool-using agents, the egress guard — BR-012).

---

## 5. Cost control (BR-006, BR-007)

- **Per-request ceiling** on every call (§1).
- **Per-job cap:** a multi-step job (e.g. an ultraQA sweep) checks a cumulative
  cost cap between steps; on hit, it stops and persists partial results rather
  than overrunning budget (BR-007).
- **Right-size the model** per task: a cheap/fast model for high-volume
  classification, a stronger model for nuanced reasoning — selected via settings,
  never hardcoded.
- Cost is recorded on each operation and surfaced via structlog.

---

## 6. Tracing (optional, off by default)

structlog JSON is always on. OpenTelemetry tracing is opt-in
(`OTEL_EXPORTER_OTLP_ENDPOINT`, via the agentkit `observability` extra). When
enabled, wrap each node/LLM call in a span. The agents must run correctly with
tracing **off**.

---

## Anti-patterns

- ❌ Direct gateway HTTP/SDK call bypassing `LLMClient` or the cost ceiling
- ❌ Hardcoded model name or provider; hardcoded gateway URL (use env / settings)
- ❌ Concatenating untrusted/retrieved text into the **system** prompt (injection surface)
- ❌ Writing unvalidated model output to the DB
- ❌ Global / cross-tenant retrieval
- ❌ Logging full prompts containing tenant content or any secret (BR-010)
- ❌ Raising raw provider exceptions to callers instead of typed, logged errors

---

## Related

- [business-rules.md](../../docs/business-rules.md) BR-006/007/010
- [user-stories.md](../../docs/user-stories.md) US-020/021
- [packages/agentkit/README.md](../../packages/agentkit/README.md) (`llm` module)
