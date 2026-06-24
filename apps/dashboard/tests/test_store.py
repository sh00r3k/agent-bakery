"""@spec BR-002 — dashboard store: tenant-scoped cost rollup + prune.

Store: cost rollup write-path, retention prune, and the per-agent row contract.

These exercise the SQL *contracts* (which statements fire, with which params, and
the row shapes the read functions emit) via a recording fake pool — no real
Postgres. The SQL math itself (GREATEST per-day, window FILTERs) is asserted by
inspecting the emitted statements rather than executing them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg import sql as psql
from dashboard import store


class _RecordingCursor:
    def __init__(self, rows: list[tuple[Any, ...]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _RecordingConn:
    def __init__(self, pool: _RecordingPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _RecordingConn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: Any, params: tuple[Any, ...] = ()) -> _RecordingCursor:
        self._pool.calls.append((sql, params))
        return _RecordingCursor(self._pool.rows, self._pool.rowcount)


class _RecordingPool:
    """Records every (sql, params) and returns canned rows / rowcount."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None, rowcount: int = 0) -> None:
        self.rows = rows or []
        self.rowcount = rowcount
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def connection(self) -> _RecordingConn:
        return _RecordingConn(self)


# --- BUG FIX: cost_agent_daily row contract ---------------------------------
@pytest.mark.asyncio
async def test_cost_agent_daily_keys_row_by_agent() -> None:
    """The per-agent rollup must label its series ``agent`` (the dimension it
    splits on). It keeps a ``model`` mirror for the shared chart builder, but the
    semantic key is ``agent`` and both hold the AGENT name, never a model."""
    day = datetime(2026, 6, 19, tzinfo=UTC)
    pool = _RecordingPool([(day, "security", 4.0), (day, "monitoring", 1.0)])
    rows = await store.cost_agent_daily(pool, days=14)
    assert rows == [
        {"day": day, "agent": "security", "model": "security", "usd": 4.0},
        {"day": day, "agent": "monitoring", "model": "monitoring", "usd": 1.0},
    ]
    # reads the rollup, filtered to the agent-total sentinel — not a raw event scan
    sql, params = pool.calls[0]
    assert "FROM cost_daily" in sql
    assert "cost_model_events" not in sql and "cost_events" not in sql
    assert params == (store._AGENT_TOTAL_MODEL, 14)


@pytest.mark.asyncio
async def test_cost_model_daily_excludes_agent_total_sentinel() -> None:
    day = datetime(2026, 6, 19, tzinfo=UTC)
    pool = _RecordingPool([(day, "gpt-5", 2.0)])
    rows = await store.cost_model_daily(pool, days=7)
    assert rows == [{"day": day, "model": "gpt-5", "usd": 2.0}]
    sql, params = pool.calls[0]
    assert "FROM cost_daily" in sql
    assert "model <> %s" in sql
    assert params == (store._AGENT_TOTAL_MODEL, 7)


# --- ROLLUP write path ------------------------------------------------------
@pytest.mark.asyncio
async def test_record_cost_folds_agent_total_into_rollup() -> None:
    pool = _RecordingPool()
    when = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    await store.record_cost(pool, agent="security", usd_today=3.5, ts=when)
    # one raw-event insert + one rollup upsert (under the sentinel model)
    assert len(pool.calls) == 2
    raw_sql, _ = pool.calls[0]
    roll_sql, roll_params = pool.calls[1]
    assert "INSERT INTO cost_events" in raw_sql
    assert "INSERT INTO cost_daily" in roll_sql and "GREATEST" in roll_sql
    assert roll_params == ("security", store._AGENT_TOTAL_MODEL, when, 3.5)


@pytest.mark.asyncio
async def test_record_cost_by_model_folds_each_model_into_rollup() -> None:
    pool = _RecordingPool()
    when = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    await store.record_cost_by_model(
        pool, agent="security", by_model={"gpt-5": 2.0, "minimax-m3": 1.0}, ts=when
    )
    # per model: one raw insert + one rollup upsert => 4 calls
    assert len(pool.calls) == 4
    rollup_calls = [c for c in pool.calls if "INSERT INTO cost_daily" in c[0]]
    assert len(rollup_calls) == 2
    assert all("GREATEST" in sql for sql, _ in rollup_calls)
    folded = {params[1]: params[3] for _, params in rollup_calls}
    assert folded == {"gpt-5": 2.0, "minimax-m3": 1.0}


@pytest.mark.asyncio
async def test_record_cost_by_model_relabels_empty_key_away_from_sentinel() -> None:
    """An untrusted upstream per-model key that is blank/empty must NOT be written
    under the agent-total sentinel PK (agent, '', day) — it would inflate the
    per-agent total via GREATEST and drop from the per-model chart. It is relabelled
    to 'unknown' before the rollup upsert so the sentinel row is never touched."""
    pool = _RecordingPool()
    when = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    await store.record_cost_by_model(pool, agent="security", by_model={"": 5.0, "  ": 2.0}, ts=when)
    rollup_calls = [c for c in pool.calls if "INSERT INTO cost_daily" in c[0]]
    # No rollup upsert may carry the agent-total sentinel model ('').
    assert all(params[1] != store._AGENT_TOTAL_MODEL for _, params in rollup_calls)
    # Both blank keys collapse to the 'unknown' label instead.
    assert all(params[1] == store._UNKNOWN_MODEL for _, params in rollup_calls)
    # The raw cost_model_events inserts are likewise relabelled (never the sentinel).
    raw_calls = [c for c in pool.calls if "INSERT INTO cost_model_events" in c[0]]
    assert all(params[1] == store._UNKNOWN_MODEL for _, params in raw_calls)


@pytest.mark.asyncio
async def test_record_cost_by_model_empty_is_noop() -> None:
    pool = _RecordingPool()
    await store.record_cost_by_model(pool, agent="security", by_model={})
    assert pool.calls == []


# --- READS draw from the rollup, not the raw event tables -------------------
@pytest.mark.asyncio
async def test_cost_windows_reads_rollup_not_event_scan() -> None:
    day = datetime(2026, 6, 19, tzinfo=UTC)
    pool = _RecordingPool([("security", 1.5, 4.0, 12.0, day)])
    rows = await store.cost_windows(pool)
    assert rows[0]["agent"] == "security"
    assert rows[0]["all_time"] == 12.0
    assert rows[0]["last_active"] == day
    sql, params = pool.calls[0]
    assert "FROM cost_daily" in sql
    assert "cost_events" not in sql  # no full scan of the raw log
    assert params == (7, 30, store._AGENT_TOTAL_MODEL)


@pytest.mark.asyncio
async def test_cost_series_reads_rollup() -> None:
    day = datetime(2026, 6, 19, tzinfo=UTC)
    pool = _RecordingPool([("security", day, 4.0)])
    rows = await store.cost_series(pool, days=7)
    assert rows == [{"agent": "security", "day": day, "usd": 4.0}]
    sql, params = pool.calls[0]
    assert "FROM cost_daily" in sql
    assert params == (store._AGENT_TOTAL_MODEL, 7)


# --- RETENTION prune --------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_old_data_deletes_three_raw_tables() -> None:
    pool = _RecordingPool(rowcount=5)
    counts = await store.prune_old_data(pool, days=30)
    assert counts == {"heartbeats": 5, "cost_events": 5, "cost_model_events": 5}
    # Three DELETE calls for the three raw tables, each with days param.
    delete_calls = [
        (sql_obj, params) for sql_obj, params in pool.calls if "DELETE FROM" in str(sql_obj)
    ]
    assert len(delete_calls) == 3
    days_params = [params[0] for _, params in delete_calls]
    assert days_params == [30, 30, 30]
    # the rollup table is NEVER pruned (it holds the long-term cost history)
    assert all("cost_daily" not in str(sql_obj) for sql_obj, _ in pool.calls)


@pytest.mark.asyncio
async def test_prune_old_data_default_retention_is_90_days() -> None:
    pool = _RecordingPool(rowcount=0)
    await store.prune_old_data(pool)
    assert all(params == (90,) for _, params in pool.calls)
    assert store.DEFAULT_RETENTION_DAYS == 90


@pytest.mark.asyncio
async def test_prune_old_data_none_pool_is_noop() -> None:
    assert await store.prune_old_data(None) == {}


@pytest.mark.asyncio
async def test_rollup_cost_daily_recomputes_both_levels() -> None:
    pool = _RecordingPool()
    await store.rollup_cost_daily(pool)
    assert len(pool.calls) == 2
    # agent-total derived from cost_events under the sentinel model
    assert "FROM cost_events" in pool.calls[0][0]
    assert pool.calls[0][1] == (store._AGENT_TOTAL_MODEL,)
    # per-model derived from cost_model_events
    assert "FROM cost_model_events" in pool.calls[1][0]
    assert all("GREATEST" in sql for sql, _ in pool.calls)
