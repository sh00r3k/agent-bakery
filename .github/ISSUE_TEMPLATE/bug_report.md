---
name: Bug report
description: Report something that is broken in agent-bakery.
labels: ["bug"]
assignees: []
---

### What happened

Describe the observed vs. expected behavior. Concrete, factual. "I tried X
and it didn't work" is not a repro.

### Reproduction

Minimal steps. Include the exact commands you ran, the env vars you set
(use `acme` / `demo` / `your-gateway.example.com` as placeholders for real
values), and the input you gave to the agent.

### Environment

- Commit SHA or tag: (run `git rev-parse HEAD`)
- Output of `uv run python -c "import agentkit; print(agentkit.__version__)"`
  (and the version of the affected member, if not `agentkit`)
- OS and Python version (`uv run python --version`)
- LLM gateway type (LiteLLM / vLLM / Ollama / OpenAI / other)
- Postgres / Redis / RabbitMQ versions (only if relevant to the bug)

### Logs / trace

Paste the relevant `structlog` JSON lines or the full traceback.
**Redact secrets, real tenant names, real customer data, and production URLs
before pasting.** Use the `acme` / `demo` / `your-gateway.example.com`
placeholders that the project uses in its own examples.

### Severity

- [ ] Blocker — system is unusable
- [ ] High — major feature broken
- [ ] Medium — feature degraded, workaround exists
- [ ] Low — cosmetic, docs, minor
