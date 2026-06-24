"""Run-heartbeat helper for scheduled / batch agents.

Plan 0 §4 (Phase 2) + Plan 2 §1.4: agents without an always-on HTTP surface
(security's daily scan, web-ext's batch pipeline, pm's daily digest) cannot be
probed for "did the last run succeed?". They each write a heartbeat row at the
end of a run; the meta-monitoring agent reads the latest row **cross-DB**
(read-only, heartbeat tables only) and pages if it is stale or failed.

One small table per agent DB, named per job family (default ``run_heartbeats``):

    CREATE TABLE run_heartbeats (
        job   text PRIMARY KEY,   -- 'scan', 'pipeline', 'digest', ...
        ts    timestamptz NOT NULL,
        status text NOT NULL,     -- 'ok' | 'partial' | 'failed' | 'started' | ...
        meta  jsonb NOT NULL DEFAULT '{}'::jsonb
    );

``beat`` is an UPSERT keyed on ``job`` so the row always reflects the latest run
for that family (history lives in the agent's own run tables, not here). All SQL
is parameterized (psycopg3 ``%s``) — never string-interpolated (AR-2).

The pool argument is a ``psycopg_pool.AsyncConnectionPool`` (as built by
``agentkit.db.pg_pool``); only ``pool.connection()`` + ``conn.execute`` are used,
so a fake pool in tests needs just those.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from psycopg import sql

from agentkit.observability import get_logger

log = get_logger("agentkit.heartbeat")

DEFAULT_TABLE = "run_heartbeats"


def _ident(table: str) -> sql.Identifier:
    """Validate + quote the table identifier.

    The table name is operator/agent-supplied (not request data), but we still
    refuse anything that isn't a plain ``[A-Za-z0-9_]`` identifier and quote it
    with ``psycopg.sql.Identifier`` so it can never be an injection vector even
    if a caller passes something dynamic. Values always go through ``%s``.
    """
    if not table or not table.replace("_", "").isalnum():
        raise ValueError(f"invalid heartbeat table name: {table!r}")
    return sql.Identifier(table)


async def create_heartbeat_table(pool: Any, *, table: str = DEFAULT_TABLE) -> None:
    """Create the heartbeat table if it does not exist. Idempotent."""
    stmt = sql.SQL(
        "CREATE TABLE IF NOT EXISTS {tbl} ("
        "  job text PRIMARY KEY,"
        "  ts timestamptz NOT NULL,"
        "  status text NOT NULL,"
        "  meta jsonb NOT NULL DEFAULT '{{}}'::jsonb"
        ")"
    ).format(tbl=_ident(table))
    async with pool.connection() as conn:
        await conn.execute(stmt)
    log.info("heartbeat.table_ready", table=table)


async def beat(
    pool: Any,
    job: str,
    status: str,
    *,
    meta: dict[str, Any] | None = None,
    ts: datetime | None = None,
    table: str = DEFAULT_TABLE,
    ensure_table: bool = True,
) -> None:
    """Record (upsert) the latest run heartbeat for ``job``.

    ``status`` is free-form but the agentkit convention is ok|partial|failed|started.
    ``meta`` is any JSON-safe dict (counts, error, duration). ``ts`` defaults to
    now (UTC).

    Defensive ordering: ``ensure_table`` (default True) runs a
    ``CREATE TABLE IF NOT EXISTS`` first, so a first beat at the END of a batch
    run cannot raise an undefined-table error if the caller never ran
    :func:`create_heartbeat_table` at startup. It is idempotent and cheap; pass
    ``ensure_table=False`` to skip it when the table is known to exist.
    """
    when = ts or datetime.now(UTC)
    payload = json.dumps(meta or {})
    if ensure_table:
        await create_heartbeat_table(pool, table=table)
    stmt = sql.SQL(
        "INSERT INTO {tbl} (job, ts, status, meta) VALUES (%s, %s, %s, %s::jsonb) "
        "ON CONFLICT (job) DO UPDATE SET ts = EXCLUDED.ts, "
        "status = EXCLUDED.status, meta = EXCLUDED.meta"
    ).format(tbl=_ident(table))
    async with pool.connection() as conn:
        await conn.execute(stmt, (job, when, status, payload))
    log.info("heartbeat.beat", job=job, status=status)


async def last_beat(pool: Any, job: str, *, table: str = DEFAULT_TABLE) -> dict[str, Any] | None:
    """Return the latest heartbeat for ``job`` as
    ``{"job", "ts", "status", "meta"}`` (``ts`` is a ``datetime``), or ``None``.
    """
    stmt = sql.SQL("SELECT job, ts, status, meta FROM {tbl} WHERE job = %s").format(
        tbl=_ident(table)
    )
    async with pool.connection() as conn:
        cur = await conn.execute(stmt, (job,))
        row = await cur.fetchone()
    if row is None:
        return None
    job_v, ts_v, status_v, meta_v = row
    return {"job": job_v, "ts": ts_v, "status": status_v, "meta": meta_v or {}}
