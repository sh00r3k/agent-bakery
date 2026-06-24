# Testing Conventions — agent-bakery

> Patterns for tests across your agents. Referenced from [AGENTS.md](../../AGENTS.md).

## Stack

- **pytest** + **pytest-asyncio** (`asyncio_mode = auto`) — runner
- **asgi-lifespan** + `httpx.ASGITransport` — drive a member's FastAPI app
  in-process, no network
- Fakes/stubs for every external dependency (LLM gateway, Postgres, Redis,
  RabbitMQ) — see §2

---

## 1. Tests are organized by spec hierarchy, not code layout

```
<member>/tests/
├── stories/        # one file per user story — header: # @spec US-NNN
│   └── test_us014_explore.py
├── rules/          # one file per business rule — header: # @spec BR-NNN
│   └── test_br012_guard.py
├── domain/         # entity invariants + state machines
│   └── test_incident_state_machine.py
├── fixtures/       # shared builders (tenants acme/demo, signals, findings)
└── conftest.py     # fakes + app factory wiring
```

Every test module starts with a `@spec` marker tying it to a US/BR/entity. This
is the verification trace that [domain-model.md](../../docs/domain-model.md),
[user-stories.md](../../docs/user-stories.md), and
[business-rules.md](../../docs/business-rules.md) point back at.

---

## 2. Offline by default — fake the externals

CI and local runs must pass **with no gateway, no live DB, no broker**. Provide
fakes:

- **LLM** — a `FakeLLMClient` returning canned structured responses and a fixed
  `cost_usd`; expose a knob to push cost over the ceiling (to test BR-006) and to
  return malformed output (to test the validate-and-retry path).
- **DB** — an in-memory repository implementing the same protocol as the psycopg
  repo, **or** a real Postgres via a container in a separately-marked integration
  job. Unit tests use the in-memory fake; isolation/audit tests may use the real
  schema.
- **Redis / RabbitMQ** — a fake pub/sub and a fake `notify` publisher that records
  published alerts (assert exactly-once in BR-009 tests).

```python
async def test_cost_ceiling_aborts(fake_llm):  # @spec BR-006
    fake_llm.next_cost(0.99)                    # push the next call over the ceiling
    with pytest.raises(CostCeilingExceeded):
        await fake_llm.chat(messages=[...])
```

No real API keys, no real PII, no business identifiers in fixtures — generic
`acme`/`demo` only (BR-010).

---

## 3. Per-tenant isolation tests are mandatory (BR-002)

Any path that reads or writes tenant-scoped agent data needs an isolation test:

- an `operator`/`end-user` principal for `acme` requesting `demo` data → **403 /
  not-found**;
- an `ops` principal reading across tenants → **allowed** (US-013), with the log
  assertion;
- a tenant-scoped query for `acme` returns **zero** `demo` rows.

The audit SQL queries in `domain-model.md` / `business-rules.md` (each "must
return 0 rows") should each have a corresponding integration test that asserts
the invariant holds after a representative sequence of operations.

---

## 4. State machines & idempotency

- `Incident`: N matching Signals fold into **one firing** Incident and publish
  **exactly one** alert (BR-009); a cleared condition resolves it.
- `Finding` (ultraQA): a repeat observation of the same `dedup_key` **upserts**
  (bumps `count`), never forks (BR-015).
- ultraQA guard: a mutating verb not on the safe-write allowlist is **never
  forwarded** to the SUT (BR-012).

---

## 5. Keep every suite green

- Each member's suite is self-contained: `uv run pytest <member>/tests` passes on
  its own. A change in one member must not break another's suite.
- Run the touched member's `pytest` + `ruff check` + `mypy` before a PR; the CI
  matrix runs all members.
- A failing or skipped test is not "temporarily fine" — fix it or revert.
- New behavior lands **with** its `@spec` test (spec ahead of code): a US/BR
  without a test is dead spec.

---

## What MUST be tested

- ✅ BR-002 (isolation + ops exception), BR-006/007 (cost ceiling/cap), BR-009
  (dedup + alert-once), BR-011/012/013 (ultraQA fail-closed guard), BR-015/016
  (finding dedup + severity enum)
- ✅ Every US happy path + its rejection path (Given/When/Then in
  [user-stories.md](../../docs/user-stories.md))
- ✅ Entity invariants + both state machines (domain-model.md)
- ✅ Error paths: gateway failure, validation failure, ceiling exceeded, bad
  signature
- ✅ `create_app()` contract — `/healthz` `/readyz` `/metrics.json` respond (US-012)

## What NOT to test

- ❌ Third-party library behavior (faked)
- ❌ Trivial config getters / Pydantic field defaults
- ❌ `assert x is not None` with no behavioral meaning
- ❌ Type-level correctness (mypy covers it)

---

## Related

- [business-rules.md](../../docs/business-rules.md) (audit queries → tests)
- [domain-model.md](../../docs/domain-model.md) (invariants, state machines)
- [llm.md](llm.md) (FakeLLMClient patterns)
