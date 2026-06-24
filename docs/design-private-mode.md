# Design — Private mode (zero outbound except configured LLM gateway)

**Date:** 2026-06-18
**Status:** ✅ **Implemented** in commits `9d3a4a1`, `d6479f4` (v0.2).
The httpx-bypass skeptic finding in §"What changes when PRIVATE_MODE is on"
was addressed by adding `GuardedTransport` (a custom
`httpx.AsyncHTTPTransport` wrapper that rejects forbidden hosts before
the inner transport runs). The CI tests in
`packages/agentkit/tests/test_egress.py` cover both stdlib and httpx
paths.
**Refs:** the "no phone-home" framing common to on-prem agent tooling; BR-006 cost ceiling; `packages/agentkit/src/agentkit/observability.py`; `packages/agentkit/src/agentkit/llm.py`

## Why

A common on-prem selling point is "no telemetry, no phone-home". Several regulated-industry / on-prem-only personas (defense, healthcare, finance, EU sovereign workloads) want **zero outbound network from the agent host, period** — beyond even the "no LangGraph Platform" claim we already make.

A **private mode** turns this into a verifiable property: with `PRIVATE_MODE=true`, the agent process opens outbound TCP connections **only** to the configured `LLM_BASE_URL` (and even that can be local Ollama → zero outbound).

## What changes when PRIVATE_MODE is on

Exhaustive list. "Blocked" = the code path is unreachable; "Allowed only to" = the code path exists but is constrained.

| Component                | Behavior                                                                                                                                                                                 |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OpenTelemetry exporter   | **Blocked** — `OTLPSpanExporter` not instantiated; if `OTEL_EXPORTER_OTLP_*` is set, the process logs a startup warning and ignores it                                                   |
| Sentry / error reporters | n/a (we don't ship one; document the assumption: any future error reporter MUST honor PRIVATE_MODE)                                                                                      |
| structlog                | **Allowed** — but **only to local file sink** (rotated, size-capped). No syslog, no TCP/UDP, no journald-external                                                                        |
| RabbitMQ                 | **Allowed only to** the broker host in `RABBITMQ_URL` — must be local (no external broker). Document this: PRIVATE_MODE + external broker is a misconfiguration                          |
| LLM gateway              | **Allowed only to** the host in `LLM_BASE_URL`. If you set `LLM_BASE_URL` to a hosted service, PRIVATE_MODE does NOT forbid that — but you can override to local Ollama for true air-gap |
| pgvector / Postgres      | **Allowed only to** the local DB. Backups (pg_dump) are out of scope; operator's choice                                                                                                  |
| Redis                    | **Allowed only to** the local Redis. Document: PRIVATE_MODE + external Redis = misconfiguration                                                                                          |
| Update / version check   | n/a (we don't ship one; document)                                                                                                                                                        |
| "Heartbeat" / phone-home | n/a (we don't ship any; document)                                                                                                                                                        |

## Config surface

Add to `BaseAgentSettings` (in `packages/agentkit/src/agentkit/config.py`):

```python
PRIVATE_MODE: bool = False  # default off; opt-in per-deployment
```

Derived settings (computed, not user-overridable):

```python
@property
def otel_enabled(self) -> bool:
    return (not self.PRIVATE_MODE) and bool(self.OTEL_EXPORTER_OTLP_ENDPOINT)
```

**Hardening rule:** in `observability.init()`, if `PRIVATE_MODE` is set AND `OTEL_EXPORTER_OTLP_ENDPOINT` is also set, log a warning at startup and **silently disable** the exporter. Do NOT refuse to start (operators with legacy env vars shouldn't be blocked).

**No escape hatch:** there is no `PRIVATE_MODE=true but still want OpenTelemetry export`. If you want tracing, leave PRIVATE_MODE=false. Document this as a deliberate non-feature.

## Per-agent override

**Cross-agent via shared `agentkit.config`.** Across your agents, different agents CAN point at different `LLM_BASE_URL` (one at hosted OpenAI, one at local Ollama) — and all can be under PRIVATE_MODE=true, because PRIVATE_MODE only forbids **other** outbound, not the LLM call itself.

Why cross-agent (not per-agent override): the threat model is "the host process opens a phone-home connection". A misconfigured single agent would re-introduce the problem. A cross-agent rule + clear documentation is the safer model. If a single agent needs trace export, that agent does not run in private mode.

## CI test (pytest sketch)

The hard part: **assert that, with `PRIVATE_MODE=true`, the test agent cannot open a TCP connection to any non-gateway host.**

Approach: a hermetic test that uses a real socket but mocks the LLM gateway at the network layer.

```python
# packages/agentkit/tests/test_private_mode.py

import socket
import threading
import pytest

@pytest.fixture
def mock_gateway():
    """Bind a real localhost socket to act as a fake LLM gateway."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    thread = threading.Thread(target=lambda: server.accept(), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.close()

def test_private_mode_blocks_external_egress(monkeypatch, mock_gateway):
    monkeypatch.setenv("PRIVATE_MODE", "true")
    monkeypatch.setenv("LLM_BASE_URL", mock_gateway)  # allowed

    # The blocked host (imagine this is example.com):
    BLOCKED = ("93.184.216.34", 80)  # example.com

    with pytest.raises(EgressBlocked):
        # Try to open a TCP connection — should be blocked.
        from agentkit.egress import guarded_connect
        guarded_connect(BLOCKED[0], BLOCKED[1])

def test_private_mode_allows_gateway(mock_gateway):
    # The configured gateway host must still be reachable.
    from agentkit.egress import guarded_connect
    host, port = mock_gateway.replace("http://", "").split(":")
    guarded_connect(host, int(port))  # should not raise

def test_private_mode_disables_otel_even_if_endpoint_set(monkeypatch):
    monkeypatch.setenv("PRIVATE_MODE", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    from agentkit.observability import init_observability
    state = init_observability()
    assert state.otel_exporter is None

def test_structlog_writes_only_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv("PRIVATE_MODE", "true")
    monkeypatch.setenv("STRUCTLOG_PATH", str(tmp_path / "agent.log"))

    from agentkit.observability import init_observability
    init_observability()

    # Assert no syslog / TCP / UDP sinks were registered
    import structlog
    cfg = structlog.get_config()
    for processor in cfg["processors"]:
        assert "Syslog" not in processor.__class__.__name__
        assert "Socket" not in processor.__class__.__name__
```

The `agentkit.egress.guarded_connect` is a thin wrapper over `socket.create_connection` that checks `PRIVATE_MODE` and rejects connections to anything not in the allow-list (`LLM_BASE_URL` host, `RABBITMQ_URL` host, `DATABASE_URL` host, `REDIS_URL` host).

This test **must be hermetic** — it must not actually contact the internet. The `BLOCKED` IP `93.184.216.34` is example.com (RFC-reserved). Use a fake IP from `192.0.2.0/24` (TEST-NET-1) or `198.51.100.0/24` (TEST-NET-2) instead to be safe — those are guaranteed unrouted.

## Modules affected

Concrete list (file paths under `packages/agentkit/src/agentkit/`):

| File                               | Change                                                                                                                   |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `config.py`                        | Add `PRIVATE_MODE: bool = False`; computed property for `otel_enabled`                                                   |
| `observability.py`                 | In `init_observability`, check `PRIVATE_MODE`; skip OpenTelemetry init; assert structlog sinks are file-only            |
| `llm.py`                           | Use `guarded_connect` instead of `httpx.AsyncClient` directly (or document that httpx calls go through the egress guard) |
| `notify.py`                        | Assert broker host is local (or document that the operator chose remote)                                                 |
| `egress.py` (NEW)                  | `guarded_connect(host, port) -> socket.socket`. Raises `EgressBlocked` if PRIVATE_MODE and host not in allow-list        |
| `db.py`                            | Already uses a pool — assert pool host matches `DATABASE_URL`; otherwise the egress guard blocks the connect             |
| `tests/test_private_mode.py` (NEW) | The hermetic CI test above                                                                                               |

Estimated effort: ~6 person-hours.

## Migration

Existing deployments — does enabling PRIVATE_MODE on a previously-default deployment break anything?

**Honest answer: probably not, if the deployment is already on the recommended stack (Postgres + Redis + RabbitMQ + Ollama all local). If it's using hosted services, enabling PRIVATE_MODE will break those connections.**

Operator checklist before enabling:

```bash
# 1. Confirm LLM_BASE_URL is local or you're OK with the outbound
echo "LLM_BASE_URL=$LLM_BASE_URL"

# 2. Confirm broker is local
echo "RABBITMQ_URL=$RABBITMQ_URL"

# 3. Confirm DB and Redis are local
echo "DATABASE_URL=$DATABASE_URL"
echo "REDIS_URL=$REDIS_URL"

# 4. Confirm no observability exporter is configured
unset OTEL_EXPORTER_OTLP_ENDPOINT

# 5. Enable
export PRIVATE_MODE=true
uv run python -m <agent>
```

No automatic migration. The operator's responsibility.

## Risk and edge cases

- **`LLM_BASE_URL` points to a hosted service.** PRIVATE_MODE does NOT forbid that. This is by design: the user might be OK with LLM calls going to a hosted API but NOT OK with telemetry. Document. The README line should be "Private mode: zero outbound except the configured LLM gateway" — accurate.
- **A third-party package in the dependency tree phones home on its own (e.g. an ML library with telemetry).** Out of scope — we can't audit every transitive dep. PRIVATE_MODE enforces OUR code's behavior, not our deps'. Document.
- **`PRIVATE_MODE=true` with `OTEL_EXPORTER_OTLP_ENDPOINT` set.** The agent logs a warning and silently disables the OpenTelemetry exporter. Does NOT refuse to start. (Refusing to start would block operators with legacy env vars from ever enabling private mode.)
- **Network policy at the firewall.** PRIVATE_MODE is enforced in code, not at the firewall. An operator who sets PRIVATE_MODE=true but also allows outbound HTTP at the firewall does NOT get true air-gap. Document: PRIVATE_MODE is **necessary but not sufficient** for air-gap; combine with firewall rules.
- **Multi-tenant with one tenant wanting telemetry.** NOT supported. The cross-agent rule applies. If a single tenant needs telemetry, that tenant's agent runs without PRIVATE_MODE (separate process, separate set of agents).

## One-line marketing claim

Proposed for the README:

> **Private mode:** zero outbound except the configured LLM gateway. Verified by an egress test in CI. Combine with firewall rules for true air-gap.

Sanity check against the design: this is **technically accurate** — PRIVATE_MODE blocks OpenTelemetry/structlog-external/RabbitMQ-external at the code layer; the egress test in CI proves it. The disclaimer about firewalls is honest.

## Why this matters

A documented, tested PRIVATE_MODE turns the "no phone-home" claim from marketing into a verifiable property. For regulated-industry adopters, that's the difference between "we trust you" and "we can audit you" — a level of enforcement most comparable tools don't offer.
