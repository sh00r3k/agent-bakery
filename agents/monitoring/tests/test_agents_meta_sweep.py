"""@spec US-011, BR-008 — meta sweep probes wired agents + produces agent-down Signal.

Agents-coverage tests for the meta sweep + snapshot.

Verifies the agents are actually wired into the meta sweep:

- ``security-agent`` and ``web_ext_control`` are in the default settings;
- ``_collect_meta_signals`` probes them via the generic agentkit
  /healthz+/readyz path;
- a down agent (streak past threshold) produces the generic ``agent-down``
  Signal through the real collector wiring.

Offline: a routing MockTransport answers /healthz and /readyz per hostname; a
fake probe-state store returns a controllable streak.
"""

from __future__ import annotations

import httpx
from monitoring_agent.scheduler import MetaDeps, _collect_meta_signals
from monitoring_agent.settings import Settings


class FakeProbeState:
    """Minimal ProbeStateStore stand-in for the meta sweep collectors.

    ``endpoint_streak`` controls what record_endpoint returns so the down/streak
    gate is deterministic. record_restart/record_depth report no previous value
    (first observation) so container/queue rules stay quiet here.
    """

    def __init__(self, endpoint_streak: int = 0) -> None:
        self.endpoint_streak = endpoint_streak
        self.endpoint_calls: list[tuple[str, bool]] = []

    async def record_endpoint(self, target: str, *, ok: bool) -> int:
        self.endpoint_calls.append((target, ok))
        return 0 if ok else self.endpoint_streak

    async def record_restart(self, target: str, restart_count: int):
        return None

    async def record_depth(self, target: str, depth: int):
        return None


def _settings(**overrides) -> Settings:
    # Trim infra targets so the sweep only touches the HTTP fakes we control.
    base = dict(
        agent_endpoints={
            "security-agent": "http://security-agent:8000",
            "web_ext_control": "http://web_ext_control:8000",
        },
        watched_containers=[],
        watched_queues=[],
        heartbeat_sources={},
        host_meminfo_path="/nonexistent/meminfo",
        rabbit_mgmt_url="http://rabbit-unused:15672",
        docker_proxy_url="http://docker-unused:2375",
    )
    base.update(overrides)
    return Settings(**base)


async def _run(settings: Settings, probe_state: FakeProbeState, handler, monkeypatch):
    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    deps = MetaDeps(probe_state=probe_state, pool_for=None)
    return await _collect_meta_signals(settings, deps)


def test_default_settings_cover_agents() -> None:
    s = Settings()
    assert "security-agent" in s.agent_endpoints
    assert s.agent_endpoints["security-agent"] == "http://security-agent:8000"
    assert "web_ext_control" in s.agent_endpoints
    assert s.agent_endpoints["web_ext_control"] == "http://web_ext_control:8000"
    assert "security-agent" in s.watched_containers
    assert "web_ext_control" in s.watched_containers


async def test_meta_sweep_probes_both_agents(monkeypatch) -> None:
    ps = FakeProbeState()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/readyz":
            return httpx.Response(200, json={"checks": {"postgres": True}})
        return httpx.Response(404)

    sigs = await _run(_settings(), ps, handler, monkeypatch)
    # Everything healthy -> no Signals.
    assert sigs == []
    # Both agents had their /healthz streak recorded -> both were probed.
    probed = {t for t, _ in ps.endpoint_calls}
    assert probed == {"endpoint:security-agent", "endpoint:web_ext_control"}


async def test_meta_sweep_emits_agent_down(monkeypatch) -> None:
    # security-agent /healthz fails; streak meets threshold -> generic agent-down.
    ps = FakeProbeState(endpoint_streak=2)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "security-agent":
            raise httpx.ConnectError("refused", request=request)
        # web_ext_control stays healthy.
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"checks": {"postgres": True}})
        return httpx.Response(404)

    sigs = await _run(_settings(fail_streak_to_alert=2), ps, handler, monkeypatch)
    fps = [s.fingerprint for s in sigs]
    assert "agent-down:security-agent" in fps
    assert fps.count("agent-down:security-agent") == 1
