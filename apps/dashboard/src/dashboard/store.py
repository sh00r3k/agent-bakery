"""The dashboard's OWN Postgres state (DB ``dashboard``) — Plan 4 §3, §7.

The dashboard reads other agents over HTTP and never touches their DBs. Its own
DB holds only what no agent owns:

- ``heartbeats``  : a health time-series snapshotted from each agent's
  /metrics.json (error-rate deltas, heartbeat age, 24h status sparkline §7.2).
- ``cost_events`` : hourly snapshots of per-agent today's LLM spend for the cost
  rollup screen (§3.4 metrics-snapshot MVP).

All SQL is parameterized (psycopg3 ``%s``) — never string-interpolated (AR-2).
Schema is created idempotently at startup (no migration framework here; the DB
is small and single-owner).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from agentkit.observability import get_logger
from psycopg import sql as psql

log = get_logger("dashboard.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeats (
    id            bigserial PRIMARY KEY,
    agent         text NOT NULL,
    ts            timestamptz NOT NULL DEFAULT now(),
    up            boolean NOT NULL,
    ready         boolean,
    uptime_s      double precision,
    error_rate_5m double precision,
    requests_5m   integer
);
CREATE INDEX IF NOT EXISTS heartbeats_agent_ts ON heartbeats (agent, ts DESC);

CREATE TABLE IF NOT EXISTS cost_events (
    id            bigserial PRIMARY KEY,
    agent         text NOT NULL,
    ts            timestamptz NOT NULL DEFAULT now(),
    usd_today     double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS cost_events_agent_ts ON cost_events (agent, ts DESC);

CREATE TABLE IF NOT EXISTS cost_model_events (
    id            bigserial PRIMARY KEY,
    agent         text NOT NULL,
    model         text NOT NULL,
    ts            timestamptz NOT NULL DEFAULT now(),
    usd_today     double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS cost_model_events_ts ON cost_model_events (ts DESC);

-- Daily rollup of LLM spend. One row per (agent, model, day) holding that day's
-- spend (the MAX of the within-day cumulative ``usd_today`` snapshots). The
-- per-agent TOTAL (independent of any per-model breakdown, since not every agent
-- reports ``cost_by_model_today``) is stored under the sentinel model '' so the
-- agent-total reads (cost_series/cost_windows) and the per-model reads
-- (cost_model_daily) draw from the same small table without scanning the raw
-- event log on every overview load. Populated on the write path (see
-- ``record_cost`` / ``record_cost_by_model``) and idempotently re-derivable via
-- ``rollup_cost_daily``.
CREATE TABLE IF NOT EXISTS cost_daily (
    agent  text NOT NULL,
    model  text NOT NULL,
    day    date NOT NULL,
    usd    double precision NOT NULL,
    PRIMARY KEY (agent, model, day)
);
CREATE INDEX IF NOT EXISTS cost_daily_day ON cost_daily (day);

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id           bigserial PRIMARY KEY,
    tenant_id    text NOT NULL,
    prefix       text NOT NULL UNIQUE,
    token_hash   text NOT NULL,
    name         text NOT NULL,
    scope        text NOT NULL,
    role         text NOT NULL,
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_used_at timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX IF NOT EXISTS pat_tenant_active
    ON personal_access_tokens (tenant_id, revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS incident_triage (
    tenant_id  text NOT NULL,
    dedup_key  text NOT NULL,
    status     text NOT NULL,
    note       text,
    actor      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dedup_key)
);
"""

# Operator triage decisions the dashboard owns for monitoring incidents (the
# monitoring agent exposes no ack/resolve endpoint, so the verb lives here).
INCIDENT_TRIAGE_STATUSES = frozenset({"acknowledged", "resolved", "snoozed", "open"})

# Sentinel ``model`` in ``cost_daily`` for the per-agent TOTAL spend row (written
# by :func:`record_cost`), distinct from the per-model breakdown rows (written by
# :func:`record_cost_by_model`). The per-agent total is authoritative even when an
# agent reports no per-model split, so the two never collide.
_AGENT_TOTAL_MODEL = ""

# Default retention horizon for the raw event/heartbeat logs. The small
# ``cost_daily`` rollup is kept indefinitely; only the unbounded raw snapshot
# tables are pruned (the rollup already captures their history).
DEFAULT_RETENTION_DAYS = 90


async def create_schema(pool: Any) -> None:
    """Create the dashboard's tables if absent. Idempotent."""
    async with pool.connection() as conn:
        await conn.execute(_SCHEMA)
    log.info("store.schema_ready")


async def record_heartbeat(
    pool: Any,
    *,
    agent: str,
    up: bool,
    ready: bool | None,
    uptime_s: float | None,
    error_rate_5m: float | None,
    requests_5m: int | None,
    ts: datetime | None = None,
) -> None:
    when = ts or datetime.now(UTC)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO heartbeats "
            "(agent, ts, up, ready, uptime_s, error_rate_5m, requests_5m) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (agent, when, up, ready, uptime_s, error_rate_5m, requests_5m),
        )


async def recent_heartbeats(
    pool: Any, agent: str, *, hours: int = 24, limit: int = 288
) -> list[dict[str, Any]]:
    """Return recent heartbeats for one agent, oldest-first (for sparklines)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT ts, up, ready, error_rate_5m FROM heartbeats "
            "WHERE agent = %s AND ts > now() - make_interval(hours => %s) "
            "ORDER BY ts DESC LIMIT %s",
            (agent, hours, limit),
        )
        rows = await cur.fetchall()
    out = [{"ts": r[0], "up": r[1], "ready": r[2], "error_rate_5m": r[3]} for r in rows]
    out.reverse()
    return out


# Upsert one day's spend into the rollup. ``usd_today`` is a within-day
# CUMULATIVE snapshot, so the day's spend is the MAX snapshot seen — never the sum
# (which would double-count) and never blind overwrite (a later snapshot could be
# a fresh-day reset arriving out of order). GREATEST keeps it monotone per day.
_ROLLUP_UPSERT = (
    "INSERT INTO cost_daily (agent, model, day, usd) "
    "VALUES (%s, %s, date_trunc('day', %s::timestamptz)::date, %s) "
    "ON CONFLICT (agent, model, day) DO UPDATE SET "
    "usd = GREATEST(cost_daily.usd, EXCLUDED.usd)"
)


async def record_cost(
    pool: Any, *, agent: str, usd_today: float, ts: datetime | None = None
) -> None:
    when = ts or datetime.now(UTC)
    usd = float(usd_today or 0.0)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO cost_events (agent, ts, usd_today) VALUES (%s, %s, %s)",
            (agent, when, usd),
        )
        await conn.execute(_ROLLUP_UPSERT, (agent, _AGENT_TOTAL_MODEL, when, usd))


# Relabel for an upstream per-model key that arrives empty/blank. An empty model
# string would collide with the agent-total sentinel PK (agent, '', day) and
# corrupt the per-agent total via GREATEST while vanishing from the per-model
# chart (which filters ``model <> ''``); coalescing to a real label keeps it as an
# honest, attributable per-model row and never touches the sentinel.
_UNKNOWN_MODEL = "unknown"


async def record_cost_by_model(
    pool: Any, *, agent: str, by_model: dict[str, float], ts: datetime | None = None
) -> None:
    """Append one per-model cost snapshot row per model (same cumulative-today
    semantics as :func:`record_cost`) and fold each into the ``cost_daily`` rollup.
    No-op for an empty mapping.

    An upstream agent's ``/metrics.json`` ``cost_by_model_today`` is UNTRUSTED: a
    blank/empty model key is relabelled to ``"unknown"`` BEFORE the rollup upsert
    so it can never write the agent-total sentinel PK (``agent, '', day``) and
    inflate the per-agent total via GREATEST (or drop out of the per-model chart)."""
    if not by_model:
        return
    when = ts or datetime.now(UTC)
    async with pool.connection() as conn:
        for model, usd in by_model.items():
            # Coalesce an empty/blank upstream key to a real label so it never
            # collides with the agent-total sentinel ('' = _AGENT_TOTAL_MODEL).
            model_label = str(model).strip() or _UNKNOWN_MODEL
            usd_f = float(usd or 0.0)
            await conn.execute(
                "INSERT INTO cost_model_events (agent, model, ts, usd_today) "
                "VALUES (%s, %s, %s, %s)",
                (agent, model_label, when, usd_f),
            )
            await conn.execute(_ROLLUP_UPSERT, (agent, model_label, when, usd_f))


async def create_pat(
    pool: Any,
    *,
    tenant_id: str,
    prefix: str,
    token_hash: str,
    name: str,
    scope: str,
    role: str,
    created_by: str,
    expires_at: datetime,
) -> int:
    """Insert a personal access token row (Pattern 3); returns its id. Only the
    hash + prefix are stored — never the plaintext secret (BR-002 tenant scope
    is carried by ``tenant_id``)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO personal_access_tokens "
            "(tenant_id, prefix, token_hash, name, scope, role, created_by, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_id, prefix, token_hash, name, scope, role, created_by, expires_at),
        )
        row = await cur.fetchone()
    return int(row[0])


async def list_pats(pool: Any, *, tenant_id: str) -> list[dict[str, Any]]:
    """Tenant's tokens, newest-first. NEVER returns the hash or any secret —
    only display metadata."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, prefix, name, scope, role, created_by, created_at, "
            "expires_at, last_used_at, revoked_at "
            "FROM personal_access_tokens WHERE tenant_id = %s ORDER BY created_at DESC",
            (tenant_id,),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "prefix": r[1],
            "name": r[2],
            "scope": r[3],
            "role": r[4],
            "created_by": r[5],
            "created_at": r[6],
            "expires_at": r[7],
            "last_used_at": r[8],
            "revoked_at": r[9],
        }
        for r in rows
    ]


async def revoke_pat(pool: Any, *, tenant_id: str, token_id: int) -> bool:
    """Mark a token revoked. Tenant-scoped (``WHERE tenant_id = %s AND id = %s``)
    so one tenant can never revoke another's token. Returns True if a row was
    updated."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE personal_access_tokens SET revoked_at = now() "
            "WHERE tenant_id = %s AND id = %s AND revoked_at IS NULL",
            (tenant_id, int(token_id)),
        )
        return bool(cur.rowcount and cur.rowcount > 0)


async def iter_audit_log(
    pool: Any, *, tenant_id: str, limit: int = 50000
) -> AsyncIterator[dict[str, Any]]:
    """Stream the tenant's audit_log rows newest-first for export (Pattern 5).

    Uses a server-side named cursor so a large export does not buffer the whole
    result set in memory. Tenant-scoped (``WHERE tenant_id = %s``) — the caller
    passes the principal's tenant, NEVER a client-supplied value (BR-002).
    """
    async with pool.connection() as conn, conn.cursor(name="audit_export") as cur:
        await cur.execute(
            "SELECT id, ts, actor, action, resource, metadata FROM audit_log "
            "WHERE tenant_id = %s ORDER BY ts DESC LIMIT %s",
            (tenant_id, int(limit)),
        )
        async for r in cur:
            yield {
                "id": r[0],
                "ts": r[1],
                "actor": r[2],
                "action": r[3],
                "resource": r[4],
                "metadata": r[5],
            }


async def set_incident_triage(
    pool: Any,
    *,
    tenant_id: str,
    dedup_key: str,
    status: str,
    actor: str,
    note: str | None = None,
) -> None:
    """Upsert the operator's triage decision for one incident (keyed by the
    monitoring agent's stable ``dedup_key``). Tenant-scoped (BR-002 carries
    ``tenant_id``); the ``ops`` operator triages whatever tenant the incident
    belongs to. No-op when the pool is ``None``.

    ASSUMPTION: the monitoring agent's ``dedup_key`` is globally unique across
    tenants (it is a hash of source+signature, not a per-tenant counter), so the
    overlay can key on (ops tenant, dedup_key) while the incidents *feed* is
    cross-tenant. If that ever changes, key the overlay on the incident's own
    tenant + dedup_key instead."""
    if pool is None:
        return
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO incident_triage (tenant_id, dedup_key, status, note, actor) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, dedup_key) DO UPDATE SET "
            "status = EXCLUDED.status, note = EXCLUDED.note, actor = EXCLUDED.actor, "
            "updated_at = now()",
            (tenant_id, dedup_key, status, note, actor),
        )


async def incident_triage_map(pool: Any, *, tenant_id: str) -> dict[str, dict[str, Any]]:
    """Return ``{dedup_key: {status, note, actor, updated_at}}`` for the tenant —
    the overlay merged onto the live incidents feed. ``{}`` when the pool is
    ``None``."""
    if pool is None:
        return {}
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT dedup_key, status, note, actor, updated_at FROM incident_triage "
            "WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = await cur.fetchall()
    return {r[0]: {"status": r[1], "note": r[2], "actor": r[3], "updated_at": r[4]} for r in rows}


async def cost_series(pool: Any, *, days: int = 7) -> list[dict[str, Any]]:
    """Per-agent daily spend over the window. Reads the pre-aggregated
    ``cost_daily`` rollup (agent-total rows) — no scan of the raw ``cost_events``
    log — so an overview load is a small indexed range read, not a full scan."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT agent, day, usd FROM cost_daily "
            "WHERE model = %s AND day > (now() - make_interval(days => %s))::date "
            "ORDER BY day DESC, agent",
            (_AGENT_TOTAL_MODEL, days),
        )
        rows = await cur.fetchall()
    return [{"agent": r[0], "day": r[1], "usd": float(r[2] or 0.0)} for r in rows]


async def cost_windows(
    pool: Any, *, week_days: int = 7, month_days: int = 30
) -> list[dict[str, Any]]:
    """Per-agent spend rolled up into week / month / all-time windows.

    Reads the small ``cost_daily`` rollup (agent-total rows) rather than scanning
    the unbounded ``cost_events`` log on every overview load. ``usd_today`` is a
    within-day cumulative snapshot, so each rollup row already holds the day's
    spend (its max) and a window's spend is the sum of those daily figures.
    Windows are rolling (last N days), not calendar weeks/months. Rows are ordered
    by all-time spend, descending. Each row also carries ``last_active`` (the most
    recent day the agent spent), which answers "when did it last run". Returns
    ``[]`` when no cost data exists."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT agent, "
            "  COALESCE(sum(usd) FILTER "
            "    (WHERE day > (now() - make_interval(days => %s))::date), 0), "
            "  COALESCE(sum(usd) FILTER "
            "    (WHERE day > (now() - make_interval(days => %s))::date), 0), "
            "  COALESCE(sum(usd), 0), "
            "  max(day) "
            "FROM cost_daily WHERE model = %s "
            "GROUP BY agent ORDER BY 4 DESC NULLS LAST, agent",
            (week_days, month_days, _AGENT_TOTAL_MODEL),
        )
        rows = await cur.fetchall()
    return [
        {
            "agent": r[0],
            "week": round(float(r[1] or 0.0), 6),
            "month": round(float(r[2] or 0.0), 6),
            "all_time": round(float(r[3] or 0.0), 6),
            "last_active": r[4],
        }
        for r in rows
    ]


async def cost_model_daily(pool: Any, *, days: int = 14) -> list[dict[str, Any]]:
    """Per-(day, model) spend over the window, summed across agents, from the
    ``cost_daily`` rollup (per-model rows; the agent-total sentinel is excluded).
    Feeds the stacked daily bar chart: each day is one bar, each model a stacked
    segment. Rows are ordered by day ascending then model. Returns ``[]`` when no
    per-model data exists."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT day, model, sum(usd) AS usd FROM cost_daily "
            "WHERE model <> %s AND day > (now() - make_interval(days => %s))::date "
            "GROUP BY day, model ORDER BY day, model",
            (_AGENT_TOTAL_MODEL, days),
        )
        rows = await cur.fetchall()
    return [{"day": r[0], "model": r[1], "usd": float(r[2] or 0.0)} for r in rows]


async def cost_agent_daily(pool: Any, *, days: int = 14) -> list[dict[str, Any]]:
    """Per-(day, agent) spend over the window, from the ``cost_daily`` rollup's
    agent-total rows. Feeds the per-agent stacked daily bar chart: each day is one
    bar, each agent a stacked segment. Rows are ordered by day ascending then
    agent. Returns ``[]`` when no cost data exists.

    Each row is keyed ``agent`` (the dimension this series actually splits on);
    the legacy ``model`` alias mirrors the agent name so the shared chart builder,
    which pivots on a ``model`` field, treats the agent as the stacked series."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT day, agent, sum(usd) AS usd FROM cost_daily "
            "WHERE model = %s AND day > (now() - make_interval(days => %s))::date "
            "GROUP BY day, agent ORDER BY day, agent",
            (_AGENT_TOTAL_MODEL, days),
        )
        rows = await cur.fetchall()
    return [{"day": r[0], "agent": r[1], "model": r[1], "usd": float(r[2] or 0.0)} for r in rows]


async def rollup_cost_daily(pool: Any) -> None:
    """Idempotently (re)derive the ``cost_daily`` rollup from the raw event logs.

    The write path keeps ``cost_daily`` current, but this lets a periodic task
    repair the rollup (e.g. after a crash mid-write) and backfill before the first
    prune. Recomputes each (agent, model, day) as the MAX cumulative snapshot — the
    same monotone-per-day semantics as the write path — and upserts via GREATEST so
    it never lowers an already-recorded figure."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO cost_daily (agent, model, day, usd) "
            "SELECT agent, %s, date_trunc('day', ts)::date, max(usd_today) "
            "FROM cost_events GROUP BY agent, date_trunc('day', ts)::date "
            "ON CONFLICT (agent, model, day) DO UPDATE SET "
            "usd = GREATEST(cost_daily.usd, EXCLUDED.usd)",
            (_AGENT_TOTAL_MODEL,),
        )
        await conn.execute(
            "INSERT INTO cost_daily (agent, model, day, usd) "
            "SELECT agent, model, date_trunc('day', ts)::date, max(usd_today) "
            "FROM cost_model_events GROUP BY agent, model, date_trunc('day', ts)::date "
            "ON CONFLICT (agent, model, day) DO UPDATE SET "
            "usd = GREATEST(cost_daily.usd, EXCLUDED.usd)",
        )


async def prune_old_data(pool: Any, *, days: int = DEFAULT_RETENTION_DAYS) -> dict[str, int]:
    """Delete raw ``heartbeats`` / ``cost_events`` / ``cost_model_events`` rows
    older than ``days``. The compact ``cost_daily`` rollup is retained (it already
    carries the long-term cost history), so pruning the unbounded raw logs keeps
    the DB small without losing the charts. Returns the per-table delete counts.
    No-op-safe when the pool is ``None``."""
    if pool is None:
        return {}
    counts: dict[str, int] = {}
    async with pool.connection() as conn:
        for table in ("heartbeats", "cost_events", "cost_model_events"):
            stmt = psql.SQL(
                "DELETE FROM {tbl} WHERE ts < now() - make_interval(days => %s)"
            ).format(tbl=psql.Identifier(table))
            cur = await conn.execute(stmt, (days,))
            counts[table] = int(cur.rowcount or 0)
    log.info("store.pruned", **counts, retention_days=days)
    return counts
