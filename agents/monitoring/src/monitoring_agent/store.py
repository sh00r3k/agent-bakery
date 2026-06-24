"""Incidents repository — durable triage state in Postgres.

One row per *distinct* problem (keyed by ``dedup_key``). A repeat occurrence of
the same problem bumps ``count`` and ``last_seen`` instead of creating a new
row, which is what lets the triage graph suppress alert spam.

Raw SQL via psycopg3 with ``%s`` placeholders (parameterized — never string
interpolation). The pool comes from ``agentkit.db.pg_pool`` and is autocommit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from agentkit import get_logger
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = get_logger("monitoring_agent.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id          BIGGENERATED_PLACEHOLDER,
    dedup_key   TEXT        NOT NULL UNIQUE,
    source      TEXT        NOT NULL,
    severity    TEXT        NOT NULL,
    title       TEXT        NOT NULL,
    body        TEXT        NOT NULL DEFAULT '',
    count       INTEGER     NOT NULL DEFAULT 1,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT        NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS incidents_last_seen_idx ON incidents (last_seen DESC);
CREATE INDEX IF NOT EXISTS incidents_status_idx ON incidents (status);
""".replace("BIGGENERATED_PLACEHOLDER", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")


def make_dedup_key(source: str, fingerprint: str) -> str:
    """Stable key for a distinct problem: sha256(source + fingerprint)."""
    digest = hashlib.sha256(f"{source}\x00{fingerprint}".encode()).hexdigest()
    return digest[:32]


@dataclass
class Incident:
    id: int
    dedup_key: str
    source: str
    severity: str
    title: str
    body: str
    count: int
    first_seen: datetime
    last_seen: datetime
    status: str
    # True when this upsert created a brand-new incident row.
    is_new: bool = False
    # Seconds since the previous occurrence (None for new incidents).
    seconds_since_prev: float | None = None


class IncidentStore:
    """Async repository over the ``incidents`` table."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @classmethod
    async def create(cls, pool: AsyncConnectionPool) -> IncidentStore:
        store = cls(pool)
        await store.init_schema()
        return store

    async def init_schema(self) -> None:
        """Idempotent table creation; safe to call on every startup."""
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA)
        log.info("store.schema_ready")

    async def upsert(
        self,
        *,
        dedup_key: str,
        source: str,
        severity: str,
        title: str,
        body: str,
    ) -> Incident:
        """Insert a new incident or bump an existing one with the same key.

        Returns the resulting row plus ``is_new`` and ``seconds_since_prev`` so
        the caller (dedup node) can decide whether to suppress.
        """
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Pull the previous last_seen first so we can compute the gap.
            await cur.execute(
                "SELECT last_seen FROM incidents WHERE dedup_key = %s",
                (dedup_key,),
            )
            prev = await cur.fetchone()

            await cur.execute(
                """
                    INSERT INTO incidents (dedup_key, source, severity, title, body)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (dedup_key) DO UPDATE SET
                        count = incidents.count + 1,
                        last_seen = now(),
                        severity = EXCLUDED.severity,
                        title = EXCLUDED.title,
                        body = EXCLUDED.body,
                        status = CASE
                            WHEN incidents.status = 'resolved' THEN 'open'
                            ELSE incidents.status
                        END
                    RETURNING *
                    """,
                (dedup_key, source, severity, title, body),
            )
            row = await cur.fetchone()

        assert row is not None  # noqa: S101 - RETURNING always yields a row; narrows for mypy
        is_new = prev is None
        seconds_since_prev: float | None = None
        if prev is not None and prev["last_seen"] is not None:
            seconds_since_prev = (row["last_seen"] - prev["last_seen"]).total_seconds()

        return Incident(
            id=row["id"],
            dedup_key=row["dedup_key"],
            source=row["source"],
            severity=row["severity"],
            title=row["title"],
            body=row["body"],
            count=row["count"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            status=row["status"],
            is_new=is_new,
            seconds_since_prev=seconds_since_prev,
        )

    async def recent(self, *, limit: int = 50) -> list[Incident]:
        """Most-recently-seen incidents, newest first."""
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM incidents ORDER BY last_seen DESC LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            Incident(
                id=r["id"],
                dedup_key=r["dedup_key"],
                source=r["source"],
                severity=r["severity"],
                title=r["title"],
                body=r["body"],
                count=r["count"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                status=r["status"],
            )
            for r in rows
        ]

    async def mark_alert_failed(self, dedup_key: str) -> None:
        """Flag an incident whose alert publish exhausted its retries.

        Persisting ``status='alert_failed'`` is what makes a dropped page
        recoverable: the next sweep re-upserts the same ``dedup_key`` (status is
        preserved through the ON CONFLICT, since it is not ``resolved``), and the
        graph's ``decide`` node forces an alert re-attempt while it stays failed.
        """
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE incidents SET status = 'alert_failed' WHERE dedup_key = %s",
                (dedup_key,),
            )

    async def clear_alert_failed(self, dedup_key: str) -> None:
        """Clear the ``alert_failed`` flag once a page is delivered."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE incidents SET status = 'open' "
                "WHERE dedup_key = %s AND status = 'alert_failed'",
                (dedup_key,),
            )

    async def prune(self, *, retention_days: int) -> int:
        """Delete resolved incidents older than ``retention_days`` by last_seen.

        Bounds the otherwise-unbounded incidents history (open / alert_failed
        rows are kept — they still need attention). Returns the rows deleted.
        ``retention_days <= 0`` is a no-op (returns 0).
        """
        if retention_days <= 0:
            return 0
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM incidents "
                "WHERE status = 'resolved' AND last_seen < now() - make_interval(days => %s)",
                (retention_days,),
            )
            deleted = cur.rowcount
        log.info("store.incidents_pruned", retention_days=retention_days, deleted=deleted)
        return int(deleted)

    async def open_count_by_source(self, source: str) -> int:
        """Count open incidents for a given source (e.g. 'agent_health').

        Backs the ``GET /agents`` snapshot's per-agent open-incident number.
        """
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM incidents WHERE status = 'open' AND source = %s",
                (source,),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# Meta-monitoring scrape state (Plan 2 §4)
# --------------------------------------------------------------------------- #

PROBE_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_probe_state (
    target        TEXT PRIMARY KEY,
    fail_streak   INTEGER     NOT NULL DEFAULT 0,
    last_restart  INTEGER,
    last_depth    INTEGER,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass
class ProbeState:
    target: str
    fail_streak: int = 0
    last_restart: int | None = None
    last_depth: int | None = None


class ProbeStateStore:
    """Cross-sweep scrape state for the "≥N sweeps" / delta rules (Plan 2 §3).

    The single-shot probe in ``collectors`` is stateless; rules like "down for ≥2
    consecutive sweeps" or "RestartCount delta ≥3" need memory that survives a
    monitor restart, so it lives in Postgres (one row per ``target``), not RAM.
    All SQL parameterized via ``%s``.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @classmethod
    async def create(cls, pool: AsyncConnectionPool) -> ProbeStateStore:
        store = cls(pool)
        await store.init_schema()
        return store

    async def init_schema(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(PROBE_STATE_SCHEMA)
        log.info("store.probe_state_schema_ready")

    async def get(self, target: str) -> ProbeState:
        """Current state for ``target`` (defaults if never seen)."""
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT target, fail_streak, last_restart, last_depth "
                "FROM agent_probe_state WHERE target = %s",
                (target,),
            )
            row = await cur.fetchone()
        if row is None:
            return ProbeState(target=target)
        return ProbeState(
            target=row["target"],
            fail_streak=row["fail_streak"],
            last_restart=row["last_restart"],
            last_depth=row["last_depth"],
        )

    async def prune(self, *, retention_days: int) -> int:
        """Delete probe-state rows not updated within ``retention_days``.

        A target that has dropped out of the watch list (renamed container,
        retired endpoint/queue) otherwise leaves a stale row forever. Live
        targets are touched every sweep, so they are never pruned.
        ``retention_days <= 0`` is a no-op (returns 0).
        """
        if retention_days <= 0:
            return 0
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM agent_probe_state "
                "WHERE updated_at < now() - make_interval(days => %s)",
                (retention_days,),
            )
            deleted = cur.rowcount
        log.info("store.probe_state_pruned", retention_days=retention_days, deleted=deleted)
        return int(deleted)

    async def record_endpoint(self, target: str, *, ok: bool) -> int:
        """Update the consecutive-failure streak for an endpoint target.

        Returns the new streak (0 when ``ok``, else previous+1). The returned
        value is what the Signal builder uses for its "≥N sweeps" gate.
        """
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                    INSERT INTO agent_probe_state (target, fail_streak, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (target) DO UPDATE SET
                        fail_streak = CASE
                            WHEN %s THEN 0
                            ELSE agent_probe_state.fail_streak + 1
                        END,
                        updated_at = now()
                    RETURNING fail_streak
                    """,
                (target, 0 if ok else 1, ok),
            )
            row = await cur.fetchone()
        return int(row["fail_streak"]) if row else 0

    async def record_restart(self, target: str, restart_count: int) -> int | None:
        """Persist the latest container RestartCount; return the PREVIOUS value.

        The caller diffs (current - previous) for the crash-loop rule. None on
        first observation (no delta yet).
        """
        # Atomic read-modify-write on ONE autocommit connection: a CTE captures
        # the prior row before the upsert overwrites it, so two overlapping
        # sweeps (scheduled job vs. manual POST /meta-sweep) can't both read the
        # same prev and lose a crash-loop delta.
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                    WITH prev AS (
                        SELECT last_restart FROM agent_probe_state WHERE target = %s
                    ),
                    upsert AS (
                        INSERT INTO agent_probe_state (target, last_restart, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (target) DO UPDATE SET
                            last_restart = EXCLUDED.last_restart, updated_at = now()
                    )
                    SELECT last_restart FROM prev
                    """,
                (target, target, restart_count),
            )
            row = await cur.fetchone()
        return row["last_restart"] if row else None

    async def record_depth(self, target: str, depth: int) -> int | None:
        """Persist the latest queue depth; return the PREVIOUS value (for rising)."""
        # Atomic read-modify-write on ONE connection (see record_restart): the
        # CTE captures the prior depth before the upsert so overlapping sweeps
        # can't lose a queue-rising delta.
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                    WITH prev AS (
                        SELECT last_depth FROM agent_probe_state WHERE target = %s
                    ),
                    upsert AS (
                        INSERT INTO agent_probe_state (target, last_depth, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (target) DO UPDATE SET
                            last_depth = EXCLUDED.last_depth, updated_at = now()
                    )
                    SELECT last_depth FROM prev
                    """,
                (target, target, depth),
            )
            row = await cur.fetchone()
        return row["last_depth"] if row else None
