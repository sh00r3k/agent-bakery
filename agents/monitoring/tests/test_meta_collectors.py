"""@spec BR-008 — meta-monitoring: down/not-ready/OOM/crashloop/overdue signals.

Meta-monitoring collector tests (Plan 2).

Offline: a fake httpx transport feeds canned /healthz, /readyz, docker-proxy and
RabbitMQ-mgmt responses; heartbeat/host-vitals logic is pure. Each test pins the
fingerprint + severity a Signal builder must produce so the down-vs-not-ready-vs
-OOM-vs-crashloop-vs-overdue branches can't silently drift.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from monitoring_agent.collectors import (
    AgentEndpointResult,
    ContainerState,
    HeartbeatState,
    collect_container_states,
    collect_heartbeats,
    collect_queue_depths,
    container_to_signals,
    endpoint_to_signals,
    heartbeat_to_signal,
    host_vitals_to_signals,
    parse_meminfo,
    probe_agent_endpoint,
    queue_to_signal,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- 1.1 endpoint probe + signals ------------------------------------------ #


async def test_probe_endpoint_healthy_and_ready() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"checks": {"postgres": True, "redis": True}})

    async with _client(handler) as client:
        r = await probe_agent_endpoint("security-agent", "http://x:8000", client=client)
    assert r.healthz_ok is True
    assert r.ready is True
    assert r.failed_checks == []


async def test_probe_endpoint_down_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as client:
        r = await probe_agent_endpoint("security-agent", "http://x:8000", client=client)
    assert r.healthz_ok is False
    assert r.ready is None
    assert "ConnectError" in (r.error or "")


async def test_probe_endpoint_up_but_not_ready_parses_failed_check() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(503, json={"checks": {"postgres": False, "redis": True}})

    async with _client(handler) as client:
        r = await probe_agent_endpoint("security-agent", "http://x:8000", client=client)
    assert r.healthz_ok is True
    assert r.ready is False
    assert r.failed_checks == ["postgres"]


def test_endpoint_down_only_alerts_at_streak_threshold() -> None:
    r = AgentEndpointResult(
        "security-agent",
        "http://x",
        healthz_ok=False,
        ready=None,
        failed_checks=[],
        latency_seconds=None,
        error="refused",
    )
    # streak 1 with threshold 2 -> no Signal (single transient blip)
    assert endpoint_to_signals(r, slow_threshold_s=2.0, fail_streak=1, fail_streak_to_alert=2) == []
    # streak 2 -> critical agent-down
    sigs = endpoint_to_signals(r, slow_threshold_s=2.0, fail_streak=2, fail_streak_to_alert=2)
    assert [s.fingerprint for s in sigs] == ["agent-down:security-agent"]
    assert sigs[0].suggested_severity == "critical"
    assert sigs[0].source == "agent_health"


def test_endpoint_notready_one_signal_per_failed_check() -> None:
    r = AgentEndpointResult(
        "security-agent",
        "http://x",
        healthz_ok=True,
        ready=False,
        failed_checks=["postgres", "redis"],
        latency_seconds=0.1,
    )
    sigs = endpoint_to_signals(r, slow_threshold_s=2.0, fail_streak=1, fail_streak_to_alert=2)
    fps = {s.fingerprint for s in sigs}
    assert fps == {"agent-notready:security-agent:postgres", "agent-notready:security-agent:redis"}
    assert all(s.suggested_severity == "warning" for s in sigs)


def test_endpoint_slow_signal() -> None:
    r = AgentEndpointResult(
        "security-agent",
        "http://x",
        healthz_ok=True,
        ready=True,
        failed_checks=[],
        latency_seconds=3.5,
    )
    sigs = endpoint_to_signals(r, slow_threshold_s=2.0, fail_streak=0, fail_streak_to_alert=2)
    assert [s.fingerprint for s in sigs] == ["agent-slow:security-agent"]


def test_endpoint_healthy_no_signals() -> None:
    r = AgentEndpointResult(
        "security-agent",
        "http://x",
        healthz_ok=True,
        ready=True,
        failed_checks=[],
        latency_seconds=0.1,
    )
    assert endpoint_to_signals(r, slow_threshold_s=2.0, fail_streak=0, fail_streak_to_alert=2) == []


# --- 1.2 docker container state -------------------------------------------- #


async def test_collect_container_states_parses_inspect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[{"Id": "abc", "Names": ["/security-agent"]}])
        return httpx.Response(
            200,
            json={
                "RestartCount": 4,
                "State": {
                    "Status": "running",
                    "OOMKilled": False,
                    "Health": {"Status": "unhealthy"},
                    "ExitCode": 0,
                },
            },
        )

    async with _client(handler) as client:
        states = await collect_container_states(
            "http://proxy:2375", ["security-agent"], client=client
        )
    assert len(states) == 1
    st = states[0]
    assert st.restart_count == 4
    assert st.status == "running"
    assert st.health == "unhealthy"


async def test_collect_container_states_missing_container() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])  # /containers/json: nothing

    async with _client(handler) as client:
        states = await collect_container_states("http://proxy:2375", ["ghost"], client=client)
    assert states[0].status == "missing"


def test_container_oom_is_critical() -> None:
    st = ContainerState(
        "security-agent",
        status="running",
        oom_killed=True,
        restart_count=2,
        health=None,
        exit_code=None,
    )
    sigs = container_to_signals(prev_restart=2, cur=st, restart_loop_threshold=3)
    assert any(
        s.fingerprint == "container-oom:security-agent" and s.suggested_severity == "critical"
        for s in sigs
    )


def test_container_crashloop_on_restart_delta() -> None:
    st = ContainerState(
        "security-agent",
        status="running",
        oom_killed=False,
        restart_count=5,
        health=None,
        exit_code=None,
    )
    sigs = container_to_signals(prev_restart=1, cur=st, restart_loop_threshold=3)  # delta 4
    assert [s.fingerprint for s in sigs] == ["container-crashloop:security-agent"]
    assert sigs[0].suggested_severity == "critical"


def test_container_no_crashloop_on_first_observation() -> None:
    st = ContainerState(
        "security-agent",
        status="running",
        oom_killed=False,
        restart_count=9,
        health=None,
        exit_code=None,
    )
    # prev None -> no delta available -> no crash-loop Signal
    assert container_to_signals(prev_restart=None, cur=st, restart_loop_threshold=3) == []


def test_container_exited_is_down() -> None:
    st = ContainerState(
        "security-agent",
        status="exited",
        oom_killed=False,
        restart_count=0,
        health=None,
        exit_code=137,
    )
    sigs = container_to_signals(prev_restart=0, cur=st, restart_loop_threshold=3)
    assert [s.fingerprint for s in sigs] == ["container-down:security-agent"]


def test_container_unhealthy_is_warning() -> None:
    st = ContainerState(
        "security-agent",
        status="running",
        oom_killed=False,
        restart_count=0,
        health="unhealthy",
        exit_code=None,
    )
    sigs = container_to_signals(prev_restart=0, cur=st, restart_loop_threshold=3)
    assert [s.fingerprint for s in sigs] == ["container-unhealthy:security-agent"]
    assert sigs[0].suggested_severity == "warning"


# --- 1.4 heartbeat freshness ----------------------------------------------- #


def _now() -> datetime:
    return datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def test_heartbeat_fresh_ok_returns_none() -> None:
    h = HeartbeatState(
        "security",
        last_finished=_now() - timedelta(hours=1),
        last_status="ok",
        expected_interval_s=86400,
    )
    assert heartbeat_to_signal(h, now=_now()) is None


def test_heartbeat_overdue_warning_then_critical() -> None:
    # interval 24h, grace 1.5 -> overdue past 36h, critical past 72h
    warn = HeartbeatState(
        "security",
        last_finished=_now() - timedelta(hours=40),
        last_status="ok",
        expected_interval_s=86400,
    )
    sig = heartbeat_to_signal(warn, now=_now())
    assert sig is not None and sig.fingerprint == "batch-overdue:security"
    assert sig.suggested_severity == "warning"

    crit = HeartbeatState(
        "security",
        last_finished=_now() - timedelta(hours=80),
        last_status="ok",
        expected_interval_s=86400,
    )
    sig2 = heartbeat_to_signal(crit, now=_now())
    assert sig2 is not None and sig2.suggested_severity == "critical"


def test_heartbeat_failed_status_is_critical() -> None:
    h = HeartbeatState(
        "web-ext-pipeline",
        last_finished=_now() - timedelta(minutes=5),
        last_status="failed",
        expected_interval_s=0,
    )
    sig = heartbeat_to_signal(h, now=_now())
    assert sig is not None and sig.fingerprint == "batch-failed:web-ext-pipeline"
    assert sig.suggested_severity == "critical"


def test_heartbeat_partial_status_is_warning() -> None:
    h = HeartbeatState(
        "web-ext-pipeline",
        last_finished=_now() - timedelta(minutes=5),
        last_status="partial",
        expected_interval_s=0,
    )
    sig = heartbeat_to_signal(h, now=_now())
    assert sig is not None and sig.suggested_severity == "warning"


def test_heartbeat_on_demand_never_overdue() -> None:
    # interval 0 + ok + ancient -> still healthy (on-demand by design)
    h = HeartbeatState(
        "web-ext-pipeline",
        last_finished=_now() - timedelta(days=30),
        last_status="ok",
        expected_interval_s=0,
    )
    assert heartbeat_to_signal(h, now=_now()) is None


def test_heartbeat_never_ran_but_scheduled_is_overdue() -> None:
    h = HeartbeatState("security", last_finished=None, last_status=None, expected_interval_s=86400)
    sig = heartbeat_to_signal(h, now=_now())
    assert sig is not None and sig.fingerprint == "batch-overdue:security"


async def test_collect_heartbeats_cross_db_reads_via_last_beat() -> None:
    # Fake pool_for + a fake pool whose connection().execute() returns a canned row.
    class _Cur:
        def __init__(self, row):
            self._row = row

        async def fetchone(self):
            return self._row

    class _Conn:
        def __init__(self, row):
            self._row = row

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return _Cur(self._row)

    class _Pool:
        def __init__(self, row):
            self._row = row

        def connection(self):
            return _Conn(self._row)

    ts = _now() - timedelta(hours=1)
    pools = {
        "security": _Pool(("scan", ts, "ok", {})),
        "web_ext_pipeline": _Pool(None),  # never ran
    }

    async def pool_for(db: str):
        return pools[db]

    configs = {
        "security-agent": {
            "db": "security",
            "table": "run_heartbeats",
            "job": "scan",
            "interval_s": 86400,
        },
        "web-ext-pipeline": {
            "db": "web_ext_pipeline",
            "table": "run_heartbeats",
            "job": "pipeline",
            "interval_s": 0,
        },
    }
    states = await collect_heartbeats(pool_for, configs)
    by_agent = {s.agent: s for s in states}
    assert by_agent["security-agent"].last_status == "ok"
    assert by_agent["web-ext-pipeline"].last_finished is None


# --- 1.5 RabbitMQ queue backlog -------------------------------------------- #


async def test_collect_queue_depths_filters_to_watched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"name": "agent.alerts.telegram", "messages_ready": 250},
                {"name": "other", "messages_ready": 9},
            ],
        )

    async with _client(handler) as client:
        depths = await collect_queue_depths(
            "http://rabbit:15672", ["agent.alerts.telegram"], client=client
        )
    assert depths == {"agent.alerts.telegram": 250}


def test_queue_backlog_signal_when_over_threshold_and_rising() -> None:
    sig = queue_to_signal("agent.alerts.telegram", depth=250, prev_depth=100, threshold=100)
    assert sig is not None and sig.fingerprint == "queue-backlog:agent.alerts.telegram"
    assert sig.suggested_severity == "critical"


def test_queue_no_signal_when_draining() -> None:
    # over threshold but falling (depth < prev) -> consumer is catching up, no page
    assert queue_to_signal("q", depth=150, prev_depth=300, threshold=100) is None


def test_queue_no_signal_under_threshold() -> None:
    assert queue_to_signal("q", depth=10, prev_depth=5, threshold=100) is None


# --- host vitals ----------------------------------------------------------- #


def test_parse_meminfo_low_mem_warning() -> None:
    # ~976 MiB available: below 1536 warn but above 768 (half) -> warning
    text = (
        "MemTotal:        8000000 kB\nMemAvailable:    1000000 kB\n"
        "SwapTotal: 4000000 kB\nSwapFree: 4000000 kB\n"
    )
    mem = parse_meminfo(text)
    assert mem["MemAvailable"] == 1000000
    sigs = host_vitals_to_signals(mem, mem_avail_warn_mb=1536, swap_used_warn_mb=512)
    low = [s for s in sigs if s.fingerprint == "host-mem-low"]
    assert low and low[0].suggested_severity == "warning"


def test_parse_meminfo_critically_low_mem() -> None:
    # ~488 MiB available: below half of 1536 (768) -> critical
    text = "MemAvailable:    500000 kB\nSwapTotal: 4000000 kB\nSwapFree: 4000000 kB\n"
    sigs = host_vitals_to_signals(
        parse_meminfo(text), mem_avail_warn_mb=1536, swap_used_warn_mb=512
    )
    low = [s for s in sigs if s.fingerprint == "host-mem-low"]
    assert low and low[0].suggested_severity == "critical"


def test_host_swap_pressure_signal() -> None:
    # ~976 MiB swap used
    text = "MemAvailable:    4000000 kB\nSwapTotal: 4000000 kB\nSwapFree: 3000000 kB\n"
    sigs = host_vitals_to_signals(
        parse_meminfo(text), mem_avail_warn_mb=1536, swap_used_warn_mb=512
    )
    assert [s.fingerprint for s in sigs] == ["host-swap-pressure"]


def test_host_vitals_healthy_no_signals() -> None:
    text = "MemAvailable:    4000000 kB\nSwapTotal: 4000000 kB\nSwapFree: 4000000 kB\n"
    assert (
        host_vitals_to_signals(parse_meminfo(text), mem_avail_warn_mb=1536, swap_used_warn_mb=512)
        == []
    )
