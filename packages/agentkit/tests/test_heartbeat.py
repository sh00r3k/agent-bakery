"""@spec US-012 — cross-DB heartbeat upsert (idempotent ON CONFLICT).

Heartbeat helper tests.

Default path uses a fake async pool that records the parameterized SQL + params
(no Postgres). An optional integration test runs against a real PG only when
``AGENTKIT_TEST_PG_DSN`` is set, else it is skipped.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from agentkit import heartbeat
from psycopg import sql


class _FakeConn:
    def __init__(self, store):
        self._store = store

    async def execute(self, statement, params=None):
        # Capture the composed SQL text + params for assertions.
        text = statement.as_string(None) if isinstance(statement, sql.Composed) else str(statement)
        self._store["calls"].append((text, params))
        return _FakeCursor(self._store)


class _FakeCursor:
    def __init__(self, store):
        self._store = store

    async def fetchone(self):
        return self._store.get("fetchone")


class _FakePool:
    def __init__(self):
        self.store = {"calls": [], "fetchone": None}

    @asynccontextmanager
    async def connection(self):
        yield _FakeConn(self.store)


async def test_create_table_idempotent_sql():
    pool = _FakePool()
    await heartbeat.create_heartbeat_table(pool)
    text, params = pool.store["calls"][0]
    assert "CREATE TABLE IF NOT EXISTS" in text
    assert '"run_heartbeats"' in text  # quoted identifier
    assert params is None


async def test_beat_is_parameterized_upsert():
    pool = _FakePool()
    ts = datetime(2026, 6, 13, 4, 0, tzinfo=UTC)
    await heartbeat.beat(pool, "digest", "ok", meta={"items": 3}, ts=ts)
    # beat() defensively creates the table first, then the upsert is calls[1].
    create_text, _ = pool.store["calls"][0]
    assert "CREATE TABLE IF NOT EXISTS" in create_text
    text, params = pool.store["calls"][1]
    assert "INSERT INTO" in text
    assert "ON CONFLICT (job) DO UPDATE" in text
    # no value interpolation: every value is a %s placeholder, params carry them
    assert text.count("%s") == 4
    assert params[0] == "digest"
    assert params[1] == ts
    assert params[2] == "ok"
    assert json.loads(params[3]) == {"items": 3}


async def test_beat_defaults_meta_and_ts():
    pool = _FakePool()
    await heartbeat.beat(pool, "scan", "failed")
    _text, params = pool.store["calls"][1]
    assert params[0] == "scan"
    assert isinstance(params[1], datetime)
    assert json.loads(params[3]) == {}


async def test_beat_defensively_creates_table_first():
    # A first beat before create_heartbeat_table must not fail on an undefined
    # table: beat() runs CREATE TABLE IF NOT EXISTS, then the upsert.
    pool = _FakePool()
    await heartbeat.beat(pool, "digest", "ok")
    assert len(pool.store["calls"]) == 2
    assert "CREATE TABLE IF NOT EXISTS" in pool.store["calls"][0][0]
    assert "INSERT INTO" in pool.store["calls"][1][0]


async def test_beat_ensure_table_false_skips_create():
    # Opt out when the table is known to exist: only the upsert runs.
    pool = _FakePool()
    await heartbeat.beat(pool, "digest", "ok", ensure_table=False)
    assert len(pool.store["calls"]) == 1
    assert "INSERT INTO" in pool.store["calls"][0][0]


async def test_last_beat_none_when_missing():
    pool = _FakePool()
    pool.store["fetchone"] = None
    assert await heartbeat.last_beat(pool, "digest") is None


async def test_last_beat_maps_row():
    pool = _FakePool()
    ts = datetime(2026, 6, 13, 4, 0, tzinfo=UTC)
    pool.store["fetchone"] = ("digest", ts, "ok", {"items": 3})
    out = await heartbeat.last_beat(pool, "digest")
    assert out == {"job": "digest", "ts": ts, "status": "ok", "meta": {"items": 3}}
    text, params = pool.store["calls"][0]
    assert "WHERE job = %s" in text
    assert params == ("digest",)


def test_invalid_table_name_rejected():
    with pytest.raises(ValueError):
        heartbeat._ident("runs; DROP TABLE x")
    with pytest.raises(ValueError):
        heartbeat._ident("")


def test_custom_table_name_quoted():
    pool = _FakePool()

    async def _run():
        await heartbeat.create_heartbeat_table(pool, table="pipeline_runs")

    import asyncio

    asyncio.run(_run())
    text, _ = pool.store["calls"][0]
    assert '"pipeline_runs"' in text


@pytest.mark.skipif(
    not os.getenv("AGENTKIT_TEST_PG_DSN"),
    reason="set AGENTKIT_TEST_PG_DSN to run the real-Postgres heartbeat round-trip",
)
async def test_heartbeat_roundtrip_real_pg():
    from psycopg_pool import AsyncConnectionPool

    dsn = os.environ["AGENTKIT_TEST_PG_DSN"]
    table = "run_heartbeats_test"
    pool = AsyncConnectionPool(
        conninfo=dsn, min_size=1, max_size=2, open=False, kwargs={"autocommit": True}
    )
    await pool.open(wait=True)
    try:
        await heartbeat.create_heartbeat_table(pool, table=table)
        await heartbeat.beat(pool, "digest", "ok", meta={"items": 3}, table=table)
        out = await heartbeat.last_beat(pool, "digest", table=table)
        assert out is not None
        assert out["status"] == "ok"
        assert out["meta"] == {"items": 3}
        # upsert: second beat overwrites
        await heartbeat.beat(pool, "digest", "failed", table=table)
        out2 = await heartbeat.last_beat(pool, "digest", table=table)
        assert out2["status"] == "failed"
        async with pool.connection() as conn:
            await conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(table)))
    finally:
        await pool.close()
