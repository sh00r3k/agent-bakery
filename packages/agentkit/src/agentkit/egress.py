"""Network egress guard for Private Mode.

When :attr:`BaseAgentSettings.private_mode` is true, every outbound TCP
connection the agent process opens must be to a host in the
**egress allow-list**: ``LLM_BASE_URL``, ``DATABASE_URL``, ``REDIS_URL``,
``RABBITMQ_URL``. Anything else (OTel collector, Sentry, an
unrelated webhook) raises :class:`EgressBlocked` BEFORE the socket opens,
so a misconfigured export cannot phone home even if the underlying
client library is buggy.

Two layers because the project's HTTP client is ``httpx``, which has its
own transport layer that stdlib socket monkey-patching does NOT intercept
(skeptic finding during the ultracode synthesis; see
``docs/design-private-mode.md`` §"httpx bypass of stdlib socket guards"):

- :func:`guarded_connect` — stdlib ``socket.create_connection`` wrapper
- :func:`guarded_httpx_client` / :class:`GuardedTransport` — wraps
  ``httpx.AsyncHTTPTransport`` with an allow-list check

Both share :class:`AllowList` so the rules are defined once.

**Mutating-verb default-deny (BR-012):** When Private Mode is on,
:class:`GuardedTransport` also inspects the HTTP method of each request.
Mutating verbs (POST, PUT, DELETE, PATCH) are **blocked by default**
unless the destination host is explicitly listed in
``safe_write_hosts``. Read-only verbs (GET, HEAD, OPTIONS) pass through
to any allowlisted host. This prevents a compromised or hallucinating
agent from issuing destructive commands to external systems.

References:
- docs/design-private-mode.md §"What changes when PRIVATE_MODE is on"
- docs/design-private-mode.md §"CI test (pytest sketch)"
- BR-006 (LLM cost ceiling; orthogonal, same client)
- BR-012 (mutating verbs default-denied by egress guard)
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    import httpx

    from agentkit.config import BaseAgentSettings


_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class MutatingVerbBlocked(RuntimeError):
    """Raised when Private Mode blocks a mutating HTTP verb (BR-012).

    The egress guard blocks POST/PUT/DELETE/PATCH by default; only hosts
    explicitly listed in ``safe_write_hosts`` may receive mutating requests.
    """

    def __init__(
        self, method: str, host: str, port: int, safe_write_hosts: tuple[str, ...]
    ) -> None:
        self.method = method
        self.host = host
        self.port = port
        self.safe_write_hosts = safe_write_hosts
        super().__init__(
            f"Private Mode blocked mutating {method} to {host}:{port}; "
            f"safe-write hosts: {safe_write_hosts or '(none)'}"
        )


# Sentinel exception used by every guarded network primitive.
class EgressBlocked(RuntimeError):
    """Raised when Private Mode forbids an outbound connection.

    Carries enough context for the operator to understand the block:
    which host:port, which allow-list was checked, what Private Mode
    flag was active.
    """

    def __init__(self, host: str, port: int, allow_list: tuple[str, ...]) -> None:
        self.host = host
        self.port = port
        self.allow_list = allow_list
        super().__init__(
            f"Private Mode blocked egress to {host}:{port}; allowed hosts: {allow_list}"
        )


@dataclass(frozen=True)
class AllowList:
    """Pre-computed allow-list of (host, port) pairs for Private Mode egress.

    Built once from :class:`BaseAgentSettings` via :meth:`from_settings`.
    The check in :meth:`allows` is a tuple lookup, not a string compare,
    so a hostile ``LLM_BASE_URL`` cannot smuggle a secondary host via
    userinfo or path.

    ``safe_write_hosts`` lists hostnames where mutating HTTP verbs
    (POST/PUT/DELETE/PATCH) are explicitly permitted (BR-012).
    All other allowlisted hosts accept read-only verbs only.
    """

    pairs: tuple[tuple[str, int], ...]
    safe_write_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_settings(cls, settings: BaseAgentSettings) -> AllowList:
        pairs: list[tuple[str, int]] = []
        write_hosts: set[str] = set()
        for url in (
            settings.llm_base_url,
            settings.database_url,
            settings.redis_url,
            settings.rabbitmq_url,
        ):
            host, port = _split_hostport(url)
            if host is not None and port is not None:
                pairs.append((host, port))
        # LLM gateway is the only allowlisted host that may receive mutating
        # verbs (POST /v1/chat/completions, POST /v1/embeddings). DB/Redis/
        # RabbitMQ use their own protocols, not HTTP mutating verbs.
        llm_host, _ = _split_hostport(settings.llm_base_url)
        if llm_host:
            write_hosts.add(llm_host)
        # Operator-configured extra write hosts (BR-012 safe-write allowlist).
        for wh in settings.egress_safe_write_hosts:
            h, _ = _split_hostport(wh)
            if h:
                write_hosts.add(h)
        return cls(tuple(pairs), frozenset(write_hosts))

    def allows(self, host: str, port: int) -> bool:
        return (host, port) in self.pairs

    def allows_write(self, host: str) -> bool:
        return host in self.safe_write_hosts


_SCHEME_DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
    "postgresql": 5432,
    "postgres": 5432,
    "amqp": 5672,
    "amqps": 5671,
    "redis": 6379,
    "rediss": 6380,
}


def _split_hostport(url_or_hostport: str) -> tuple[str | None, int | None]:
    """Parse ``http://host:port/path`` or ``host:port`` into ``(host, port)``.

    Accepts the URL forms produced by ``BaseAgentSettings`` (``https://``,
    ``postgresql://``, ``amqp://``, ``redis://``) and the bare ``host:port``
    form. When the URL has no explicit port, falls back to the scheme
    default (https→443, postgres→5432, amqp→5672, etc.) so the allow-list
    is keyed on (host, port) tuples consistently — otherwise an egress
    check that allows ``https://gateway.example.com/v1`` would silently
    refuse the connection because the parsed (host, port) has no port.
    Returns ``(None, None)`` on parse failure (the caller skips that entry
    rather than silently allowing it).
    """
    if not url_or_hostport:
        return None, None
    if "://" in url_or_hostport:
        parsed = urlparse(url_or_hostport)
        host = parsed.hostname
        port = parsed.port or _SCHEME_DEFAULT_PORTS.get(parsed.scheme)
        return host, port
    if ":" in url_or_hostport:
        host, _, port_s = url_or_hostport.rpartition(":")
        try:
            return host, int(port_s)
        except ValueError:
            return host, None
    return url_or_hostport, None


def guarded_connect(
    host: str,
    port: int,
    *,
    settings: BaseAgentSettings | None = None,
    allow_list: AllowList | None = None,
    timeout: float | None = None,
) -> socket.socket:
    """Open a TCP connection, raising :class:`EgressBlocked` if Private Mode forbids it.

    Pass EITHER ``settings`` (the guard reads ``private_mode`` and builds the
    allow-list from it) OR ``allow_list`` (callers that already built one).
    If neither is passed, the function treats ``private_mode`` as False
    and opens the connection normally. ``timeout`` is forwarded to
    ``socket.create_connection`` (None = stdlib default).
    """
    if settings is not None and not settings.private_mode:
        return socket.create_connection((host, port), timeout=timeout)
    list_ = allow_list or (AllowList.from_settings(settings) if settings is not None else None)
    if list_ is None:
        return socket.create_connection((host, port), timeout=timeout)
    if not list_.allows(host, port):
        raise EgressBlocked(host, port, tuple(p[0] for p in list_.pairs))
    return socket.create_connection((host, port), timeout=timeout)


class GuardedTransport:
    """``httpx.AsyncHTTPTransport`` wrapper that rejects forbidden hosts.

    httpx calls this transport's ``handle_async_request`` for every request.
    We resolve the destination URL to a ``(host, port)`` pair and check the
    allow-list BEFORE delegating to the inner transport. The host never
    reaches the inner transport if it's forbidden — the connection is not
    attempted.

    This is the property that proves Private Mode holds for ``agentkit.llm``,
    which uses ``openai.AsyncOpenAI`` (which uses ``httpx`` under the hood).
    stdlib socket monkey-patching is insufficient.
    """

    def __init__(
        self,
        *,
        inner: httpx.AsyncHTTPTransport,
        allow_list: AllowList,
        private_mode: bool,
    ) -> None:
        self._inner = inner
        self._allow_list = allow_list
        self._private_mode = private_mode

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._private_mode:
            return await self._inner.handle_async_request(request)
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if not self._allow_list.allows(host, port):
            raise EgressBlocked(host, port, tuple(p[0] for p in self._allow_list.pairs))
        # BR-012: mutating verbs default-denied unless host is in safe_write_hosts.
        method = request.method.upper()
        if method in _MUTATING_METHODS and not self._allow_list.allows_write(host):
            raise MutatingVerbBlocked(
                method, host, port, tuple(sorted(self._allow_list.safe_write_hosts))
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def guarded_httpx_client(
    settings: BaseAgentSettings,
    **httpx_kwargs: Any,
) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` whose transport checks the allow-list.

    When ``settings.private_mode`` is False this is just
    ``httpx.AsyncClient(**httpx_kwargs)`` — zero overhead.
    When True the transport is wrapped with :class:`GuardedTransport` so
    every outbound URL is checked against the allow-list before connect.
    """
    import httpx

    if not settings.private_mode:
        return httpx.AsyncClient(**httpx_kwargs)
    inner = httpx.AsyncHTTPTransport()
    transport = GuardedTransport(
        inner=inner,
        allow_list=AllowList.from_settings(settings),
        private_mode=True,
    )
    kwargs = dict(httpx_kwargs)
    kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)
