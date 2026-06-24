"""@spec BR-002 — audit-log rows are tenant-scoped on append/query.

Tests for the shared audit-log primitive (agentkit.audit).
"""

from __future__ import annotations

import json

import pytest
from agentkit import audit


class _FakeCur:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple], capture: dict) -> None:
        self._rows = rows
        self._capture = capture

    async def execute(self, sql: str, params=None):
        self._capture["sql"] = sql
        self._capture["params"] = params
        return _FakeCur(self._rows)

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self._rows = rows or []
        self.capture: dict = {}

    def connection(self) -> _FakeConn:
        return _FakeConn(self._rows, self.capture)


def test_schema_defines_audit_log_with_tenant() -> None:
    assert "audit_log" in audit._SCHEMA
    assert "tenant_id" in audit._SCHEMA


@pytest.mark.asyncio
async def test_append_noop_when_pool_none() -> None:
    # Must not raise.
    await audit.append(None, tenant_id="t", actor="a", action="x")


@pytest.mark.asyncio
async def test_query_returns_empty_when_pool_none() -> None:
    assert await audit.query(None, tenant_id="t") == []


@pytest.mark.asyncio
async def test_query_always_scopes_tenant_first() -> None:
    pool = _FakePool(rows=[])
    await audit.query(pool, tenant_id="platform", limit=10)
    sql = pool.capture["sql"]
    params = pool.capture["params"]
    sql_str = str(sql)
    assert "tenant_id = %s" in sql_str
    assert params[0] == "platform"


@pytest.mark.asyncio
async def test_query_action_filter_adds_clause() -> None:
    pool = _FakePool(rows=[])
    await audit.query(pool, tenant_id="platform", action="sweep")
    sql_str = str(pool.capture["sql"])
    assert "action = %s" in sql_str
    assert "sweep" in pool.capture["params"]


@pytest.mark.asyncio
async def test_append_serializes_metadata_to_json() -> None:
    pool = _FakePool()
    await audit.append(
        pool, tenant_id="t", actor="op", action="sweep", resource="incidents", metadata={"ok": True}
    )
    params = pool.capture["params"]
    # metadata is the last bound param and must be a JSON string.
    assert json.loads(params[-1]) == {"ok": True}


@pytest.mark.asyncio
async def test_query_maps_rows_to_dicts() -> None:
    rows = [(1, "t", "2026-01-01", "op", "sweep", "incidents", {"ok": True})]
    pool = _FakePool(rows=rows)
    out = await audit.query(pool, tenant_id="t")
    assert out[0]["actor"] == "op"
    assert out[0]["action"] == "sweep"
    assert out[0]["metadata"] == {"ok": True}
