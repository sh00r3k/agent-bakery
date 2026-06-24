"""@spec BR-009 — incident dedup key + ON CONFLICT upsert semantics.

Dedup tests.

- ``make_dedup_key`` must be stable and collision-resistant across sources.
- The in-memory FakeIncidentStore (mirroring the ON CONFLICT upsert) must bump
  count + report seconds_since_prev for repeats, and report is_new for fresh
  keys.

A live-Postgres integration test of the real IncidentStore is included but
skipped unless DATABASE_URL / a reachable cluster is configured.
"""

from __future__ import annotations

from monitoring_agent.store import IncidentStore, ProbeStateStore, make_dedup_key


def test_dedup_key_stable() -> None:
    a = make_dedup_key("sentry", "backend:vpn.createSubscription")
    b = make_dedup_key("sentry", "backend:vpn.createSubscription")
    assert a == b
    assert len(a) == 32


def test_dedup_key_distinct_per_source_and_fingerprint() -> None:
    assert make_dedup_key("sentry", "x") != make_dedup_key("healthcheck", "x")
    assert make_dedup_key("sentry", "x") != make_dedup_key("sentry", "y")


async def test_upsert_new_then_repeat(fake_store) -> None:
    key = make_dedup_key("healthcheck", "down:https://app.example.com")

    first = await fake_store.upsert(
        dedup_key=key, source="healthcheck", severity="critical", title="DOWN", body="b"
    )
    assert first.is_new is True
    assert first.count == 1
    assert first.seconds_since_prev is None

    fake_store.advance(seconds=120)
    second = await fake_store.upsert(
        dedup_key=key, source="healthcheck", severity="critical", title="DOWN", body="b"
    )
    assert second.is_new is False
    assert second.count == 2
    assert second.seconds_since_prev == 120
    assert second.id == first.id


async def test_upsert_distinct_keys_are_separate_rows(fake_store) -> None:
    k1 = make_dedup_key("sentry", "a")
    k2 = make_dedup_key("sentry", "b")
    i1 = await fake_store.upsert(
        dedup_key=k1, source="sentry", severity="warning", title="A", body=""
    )
    i2 = await fake_store.upsert(
        dedup_key=k2, source="sentry", severity="warning", title="B", body=""
    )
    assert i1.id != i2.id
    assert i1.is_new and i2.is_new
    recent = await fake_store.recent()
    assert {r.id for r in recent} == {i1.id, i2.id}


# --------------------------------------------------------------------------- #
# retention prune + alert-failed marking (SQL exercised live; offline asserts
# the control flow + the issued statement)
# --------------------------------------------------------------------------- #


class _RecordingCursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _RecordingConn:
    def __init__(self, sink, rowcount) -> None:
        self.sink = sink
        self._rowcount = rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params=None):
        self.sink.append((sql, params))
        return _RecordingCursor(self._rowcount)


class _RecordingPool:
    def __init__(self, rowcount: int = 7) -> None:
        self.sql: list = []
        self._rowcount = rowcount

    def connection(self):
        return _RecordingConn(self.sql, self._rowcount)


async def test_incident_prune_deletes_only_resolved_and_returns_count() -> None:
    pool = _RecordingPool(rowcount=7)
    store = IncidentStore(pool)
    deleted = await store.prune(retention_days=90)
    assert deleted == 7
    sql, params = pool.sql[0]
    assert "DELETE FROM incidents" in sql
    assert "status = 'resolved'" in sql  # open / alert_failed rows are kept
    assert params == (90,)


async def test_incident_prune_disabled_is_noop() -> None:
    pool = _RecordingPool()
    store = IncidentStore(pool)
    assert await store.prune(retention_days=0) == 0
    assert pool.sql == []  # no DELETE issued when disabled


async def test_probe_state_prune_deletes_stale_rows_and_returns_count() -> None:
    pool = _RecordingPool(rowcount=3)
    store = ProbeStateStore(pool)
    deleted = await store.prune(retention_days=30)
    assert deleted == 3
    sql, params = pool.sql[0]
    assert "DELETE FROM agent_probe_state" in sql
    assert params == (30,)


async def test_probe_state_prune_disabled_is_noop() -> None:
    pool = _RecordingPool()
    store = ProbeStateStore(pool)
    assert await store.prune(retention_days=-1) == 0
    assert pool.sql == []


async def test_mark_alert_failed_sets_status() -> None:
    pool = _RecordingPool()
    store = IncidentStore(pool)
    await store.mark_alert_failed("k1")
    sql, params = pool.sql[0]
    assert "status = 'alert_failed'" in sql
    assert params == ("k1",)


async def test_clear_alert_failed_only_clears_failed_rows() -> None:
    pool = _RecordingPool()
    store = IncidentStore(pool)
    await store.clear_alert_failed("k1")
    sql, params = pool.sql[0]
    assert "status = 'open'" in sql
    assert "status = 'alert_failed'" in sql  # guarded: only flips failed rows
    assert params == ("k1",)
