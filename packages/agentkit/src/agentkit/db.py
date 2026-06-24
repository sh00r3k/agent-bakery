"""Async Postgres pool + Redis client on the shared `agent_backend` cluster.

Each agent gets its own database (created by infra/bootstrap.sql) and its own
Redis DB index. Connection objects are created once per process via the
lifespan helpers and reused.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
from psycopg_pool import AsyncConnectionPool

from agentkit.observability import get_logger

if TYPE_CHECKING:
    import psycopg

    from agentkit.config import BaseAgentSettings

log = get_logger("agentkit.db")

# Per-check ceiling for /readyz dependency probes. A readiness probe whose whole
# purpose is to fail fast must never wedge the proxy/orchestrator health loop on
# a hung Postgres/Redis: each check is bounded and reported False on timeout.
_PING_TIMEOUT_S = 2.0


@asynccontextmanager
async def pg_pool(
    settings: BaseAgentSettings, *, min_size: int = 1, max_size: int = 10
) -> AsyncIterator[AsyncConnectionPool]:
    """Async Postgres connection pool, opened for the duration of the context.

    Wire into FastAPI lifespan::

        async with pg_pool(settings) as pool:
            app.state.pool = pool
            yield
    """
    pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True},
    )
    await pool.open(wait=True)
    log.info("db.pg_pool_open", db=settings.database_name, max_size=max_size)
    try:
        yield pool
    finally:
        await pool.close()
        log.info("db.pg_pool_closed", db=settings.database_name)


def redis_client(settings: BaseAgentSettings) -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def ping(settings: BaseAgentSettings) -> dict[str, bool]:
    """Readiness probe: are Postgres and Redis reachable? Used by /readyz.

    Each check is independently bounded by ``_PING_TIMEOUT_S`` (libpq
    connect_timeout AND an outer asyncio.wait_for) so a dep that accepts the TCP
    connection but never responds can't hang the probe forever — it reports that
    one dep False and lets the other still report its true state.
    """
    status = {"postgres": False, "redis": False}
    try:
        await asyncio.wait_for(_pg_check(settings), timeout=_PING_TIMEOUT_S)
        status["postgres"] = True
    except Exception as exc:
        log.warning("db.pg_ping_failed", error=str(exc))
    try:
        await asyncio.wait_for(_redis_check(settings), timeout=_PING_TIMEOUT_S)
        status["redis"] = True
    except Exception as exc:
        log.warning("db.redis_ping_failed", error=str(exc))
    return status


async def _pg_check(settings: BaseAgentSettings) -> None:
    async with await _one_conn(settings) as conn:
        await conn.execute("SELECT 1")


async def _redis_check(settings: BaseAgentSettings) -> None:
    r = redis_client(settings)
    try:
        await r.ping()
    finally:
        await r.aclose()


async def _one_conn(settings: BaseAgentSettings) -> psycopg.AsyncConnection[Any]:
    import psycopg

    # connect_timeout caps the libpq handshake itself (DNS/TCP/auth); the outer
    # asyncio.wait_for in ping() caps the whole check including a slow SELECT 1.
    timeout = max(1, int(_PING_TIMEOUT_S))
    return await psycopg.AsyncConnection.connect(
        settings.database_url, autocommit=True, connect_timeout=timeout
    )
