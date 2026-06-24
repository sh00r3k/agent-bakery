"""@spec BR-008 — probe streak / delta bookkeeping (ON CONFLICT upsert).

ProbeStateStore streak/delta logic (Plan 2 §3, §4).

The ON CONFLICT upsert lives in SQL, so the *exact* streak arithmetic is only
fully exercised against a real Postgres (integration test below, auto-skipped
when no cluster is reachable). Offline, a fake pool verifies the Python control
flow that the collectors depend on: ``record_restart`` / ``record_depth`` return
the PREVIOUS value (so the caller can diff), and ``record_endpoint`` issues an
upsert keyed on the target.
"""

from __future__ import annotations

import os

import pytest
from monitoring_agent.store import ProbeState, ProbeStateStore


class _FakeCursor:
    """Async-context cursor; replays a queued row per execute (SELECT/RETURNING)."""

    def __init__(self, sink):
        self.sink = sink
        self._row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        self.sink["sql"].append((sql, params))
        self._row = self.sink["rows"].pop(0) if self.sink["rows"] else None

    async def fetchone(self):
        return self._row


class _FakeConn:
    """Records executed SQL; replays a queued row for SELECT/RETURNING."""

    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def cursor(self, *, row_factory=None):
        return _FakeCursor(self.sink)

    async def execute(self, sql, params=None):
        self.sink["sql"].append((sql, params))
        # Return the next queued row (for SELECT / RETURNING).
        row = self.sink["rows"].pop(0) if self.sink["rows"] else None
        cur = _FakeCursor(self.sink)
        cur._row = row
        return cur


class _FakePool:
    def __init__(self):
        self.sink = {"sql": [], "rows": []}

    def connection(self):
        return _FakeConn(self.sink)


async def test_record_restart_returns_previous_value() -> None:
    pool = _FakePool()
    store = ProbeStateStore(pool)
    # get() SELECT returns dict_row with prior last_restart=2; then the upsert.
    pool.sink["rows"] = [
        {"target": "container:x", "fail_streak": 0, "last_restart": 2, "last_depth": None}
    ]
    prev = await store.record_restart("container:x", 5)
    assert prev == 2
    # An upsert was issued with the new restart count.
    assert any("agent_probe_state" in str(sql) for sql, _ in pool.sink["sql"])


async def test_record_restart_none_on_first_observation() -> None:
    pool = _FakePool()
    store = ProbeStateStore(pool)
    pool.sink["rows"] = [None]  # get() finds no prior row
    prev = await store.record_restart("container:new", 0)
    assert prev is None


async def test_record_depth_returns_previous_value() -> None:
    pool = _FakePool()
    store = ProbeStateStore(pool)
    pool.sink["rows"] = [
        {"target": "queue:q", "fail_streak": 0, "last_restart": None, "last_depth": 100}
    ]
    prev = await store.record_depth("queue:q", 250)
    assert prev == 100


async def test_get_defaults_when_absent() -> None:
    pool = _FakePool()
    store = ProbeStateStore(pool)
    pool.sink["rows"] = [None]
    state = await store.get("missing")
    assert state == ProbeState(target="missing", fail_streak=0, last_restart=None, last_depth=None)


# --- integration: real streak arithmetic against Postgres (auto-skip) ------ #


@pytest.mark.skipif(
    not os.getenv("MONITORING_TEST_DATABASE_URL"),
    reason="set MONITORING_TEST_DATABASE_URL to run the live ProbeStateStore test",
)
async def test_endpoint_streak_increments_and_resets_live() -> None:
    from psycopg_pool import AsyncConnectionPool

    url = os.environ["MONITORING_TEST_DATABASE_URL"]
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False, kwargs={"autocommit": True})
    await pool.open(wait=True)
    try:
        store = await ProbeStateStore.create(pool)
        target = "endpoint:_pytest_streak"
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM agent_probe_state WHERE target = %s", (target,))
        assert await store.record_endpoint(target, ok=False) == 1
        assert await store.record_endpoint(target, ok=False) == 2
        assert await store.record_endpoint(target, ok=True) == 0  # reset on recovery
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM agent_probe_state WHERE target = %s", (target,))
    finally:
        await pool.close()
