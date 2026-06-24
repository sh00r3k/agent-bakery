"""Alert publishing over the shared RabbitMQ → chat microservice.

By convention, agents never call a chat-platform API directly. They
publish a structured alert to the `agent.alerts` topic exchange; a forwarder
bridges it to the chat microservice. Routing key = severity, so consumers
can subscribe selectively (e.g. only `*.critical`).

Keep payloads small and structured — the forwarder formats the human message.

Connection reuse: opening a fresh robust AMQP connection per alert is an
anti-pattern — under an alert burst (exactly when the monitor fires) it means N
full handshakes plus connection churn on the very RabbitMQ the agents monitor.
A process-wide :class:`NotifyPool` caches one robust connection+channel and
re-publishes on it. Open it once in the FastAPI lifespan::

    from agentkit.notify import NotifyPool

    async with NotifyPool(settings.rabbitmq_url) as notifier:
        app.state.notifier = notifier
        yield

and publish via ``await app.state.notifier.publish(alert)``. The module-level
:func:`publish_alert` keeps the old ``(rabbitmq_url, alert)`` signature for
callers that don't thread a pool through; it lazily reuses a per-URL cached
``NotifyPool`` so even that path stops reconnecting per message.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from types import TracebackType
from typing import Any, Literal

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractRobustConnection

from agentkit.observability import get_logger

log = get_logger("agentkit.notify")

Severity = Literal["info", "warning", "critical"]
EXCHANGE = "agent.alerts"


@dataclass
class Alert:
    agent: str
    severity: Severity
    title: str
    body: str
    # Stable key for dedup/grouping on the consumer side (e.g. incident id).
    dedup_key: str | None = None
    url: str | None = None
    meta: dict[str, Any] | None = None


def _encode(alert: Alert) -> aio_pika.Message:
    return aio_pika.Message(
        body=json.dumps(asdict(alert)).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )


class NotifyPool:
    """Process-wide reusable robust AMQP connection+channel for alert fan-out.

    Lazily connects on first publish and self-heals: if the cached connection
    was closed (broker bounce), the next publish reconnects. Safe for
    concurrent publishers — connect is guarded by a lock so only one handshake
    happens. Best-effort like the old helper: failures are logged and re-raised
    so the caller decides whether a dropped alert should fail the run.
    """

    def __init__(self, rabbitmq_url: str) -> None:
        self._url = rabbitmq_url
        self._conn: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> AbstractChannel:
        if self._channel is not None and not self._channel.is_closed:
            return self._channel
        async with self._lock:
            # Re-check under the lock: another coroutine may have connected.
            if self._channel is not None and not self._channel.is_closed:
                return self._channel
            if self._conn is None or self._conn.is_closed:
                self._conn = await aio_pika.connect_robust(self._url)
            channel: AbstractChannel = await self._conn.channel()
            self._channel = channel
            log.info("notify.connected")
            return channel

    async def publish(self, alert: Alert) -> None:
        """Publish one alert on the reused channel (connecting if needed)."""
        channel = await self._ensure()
        exchange = await channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )
        routing_key = f"{alert.agent}.{alert.severity}"
        await exchange.publish(_encode(alert), routing_key=routing_key)
        log.info("notify.published", routing_key=routing_key, title=alert.title)

    async def close(self) -> None:
        conn, self._conn, self._channel = self._conn, None, None
        if conn is not None and not conn.is_closed:
            await conn.close()
            log.info("notify.closed")

    async def __aenter__(self) -> NotifyPool:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


# Per-URL cached pools so the legacy publish_alert(url, alert) path also reuses
# connections instead of reconnecting per message.
_pools: dict[str, NotifyPool] = {}


def _shared_pool(rabbitmq_url: str) -> NotifyPool:
    pool = _pools.get(rabbitmq_url)
    if pool is None:
        pool = NotifyPool(rabbitmq_url)
        _pools[rabbitmq_url] = pool
    return pool


async def publish_alert(rabbitmq_url: str, alert: Alert) -> None:
    """Publish one alert to the topic exchange. Best-effort: logs and re-raises
    so the caller can decide whether a failed alert should fail the run.

    Back-compat shim around :class:`NotifyPool`: reuses a process-wide cached
    connection keyed by ``rabbitmq_url`` rather than reconnecting per call.
    """
    await _shared_pool(rabbitmq_url).publish(alert)


async def close_shared_pools() -> None:
    """Close all cached per-URL pools (call from lifespan shutdown if used)."""
    pools = list(_pools.values())
    _pools.clear()
    for pool in pools:
        await pool.close()
