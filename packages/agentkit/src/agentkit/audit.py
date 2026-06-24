"""Shared audit-log primitive (Pattern 4 — activity stream).

A tiny, dependency-light module any agentkit consumer can use to append an
auditable record of a state-changing action and to query the chronological
activity stream back out. It owns the ``audit_log`` table schema.

Design contract (matches the dashboard's ``store`` conventions):

- Every row is tenant-scoped (``tenant_id`` NOT NULL) and every read filters
  ``WHERE tenant_id = %s`` FIRST (BR-002 multi-tenant isolation).
- All SQL is parameterized (psycopg3 ``%s``) or built with ``psycopg.sql.SQL``
  — never string-interpolated.
- ``append`` is BEST-EFFORT: it never raises into the caller's request path
  (an audit write failing must not break the action it records) and is a no-op
  when the pool is ``None`` (DB unreachable at boot — the established
  graceful-degrade contract).

Stdlib + psycopg3 + agentkit logging only — no FastAPI / dashboard imports, so
sibling agents can adopt it without pulling the dashboard in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from psycopg import sql as psql

from agentkit.observability import get_logger

log = get_logger("agentkit.audit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id        bigserial PRIMARY KEY,
    tenant_id text NOT NULL,
    ts        timestamptz NOT NULL DEFAULT now(),
    actor     text NOT NULL,
    action    text NOT NULL,
    resource  text,
    metadata  jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_tenant_ts        ON audit_log (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_log_tenant_action_ts ON audit_log (tenant_id, action, ts DESC);
"""


async def create_audit_schema(pool: Any) -> None:
    """Create the ``audit_log`` table + indexes if absent. Idempotent, no-op
    when ``pool`` is ``None``."""
    if pool is None:
        return
    async with pool.connection() as conn:
        await conn.execute(_SCHEMA)
    log.info("audit.schema_ready")


async def append(
    pool: Any,
    *,
    tenant_id: str,
    actor: str,
    action: str,
    resource: str | None = None,
    metadata: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> None:
    """Append one audit row. Best-effort: swallows DB errors (logs a warning)
    and is a no-op when ``pool`` is ``None`` — auditing must never break the
    action it records."""
    if pool is None:
        return
    when = ts or datetime.now(UTC)
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO audit_log (tenant_id, ts, actor, action, resource, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (tenant_id, when, actor, action, resource, json.dumps(metadata or {})),
            )
    except Exception as exc:  # pragma: no cover - defensive; audit is best-effort
        log.warning("audit.append_failed", action=action, error=str(exc))


async def query(
    pool: Any,
    *,
    tenant_id: str,
    action: str | None = None,
    actor: str | None = None,
    resource: str | None = None,
    limit: int = 100,
    before: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return recent audit rows for a tenant, newest-first. Returns ``[]`` when
    the pool is ``None``. ``action``/``actor``/``resource``/``before`` narrow the
    feed (``before`` is the keyset-pagination cursor: pass the oldest shown
    ``ts`` to load the next page)."""
    if pool is None:
        return []
    clauses = ["tenant_id = %s"]
    params: list[Any] = [tenant_id]
    if action:
        clauses.append("action = %s")
        params.append(action)
    if actor:
        clauses.append("actor = %s")
        params.append(actor)
    if resource:
        clauses.append("resource = %s")
        params.append(resource)
    if before is not None:
        clauses.append("ts < %s")
        params.append(before)
    params.append(int(limit))
    where = psql.SQL(" AND ").join(psql.SQL(c) for c in clauses)
    stmt = psql.SQL(
        "SELECT id, tenant_id, ts, actor, action, resource, metadata "
        "FROM audit_log WHERE {where} ORDER BY ts DESC LIMIT %s"
    ).format(where=where)
    async with pool.connection() as conn:
        cur = await conn.execute(stmt, tuple(params))
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "tenant_id": r[1],
            "ts": r[2],
            "actor": r[3],
            "action": r[4],
            "resource": r[5],
            "metadata": r[6],
        }
        for r in rows
    ]


async def distinct_actions(pool: Any, *, tenant_id: str) -> list[str]:
    """The set of action verbs seen in this tenant's log, sorted — to populate a
    filter dropdown. Returns ``[]`` when the pool is ``None``."""
    if pool is None:
        return []
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT action FROM audit_log WHERE tenant_id = %s ORDER BY action",
            (tenant_id,),
        )
        rows = await cur.fetchall()
    return [r[0] for r in rows]
