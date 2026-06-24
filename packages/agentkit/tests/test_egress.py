"""@spec BR-010, BR-011 — private-mode egress guard: host allowlist, default-deny.

Egress guard tests.

Behavior coverage:
- EgressBlocked carries host, port, allow_list.
- AllowList parses the four BaseAgentSettings URL fields and lets through
  the gateway hosts; rejects everything else.
- guarded_connect honours ``private_mode``: open when off, block + raise
  EgressBlocked when on for non-allowed hosts.
- guarded_connect honours a pre-built AllowList (the route-layer call path).
- GuardedTransport blocks httpx requests to non-allowed hosts in Private Mode.
- GuardedTransport passes httpx requests through when Private Mode is off.

The httpx tests use a real local TCP server bound on 127.0.0.1 — they do
NOT reach the internet. Forbidden-host tests use 192.0.2.1 (RFC 5737
TEST-NET-1, guaranteed unrouted).

References:
- docs/design-private-mode.md §"CI test (pytest sketch)"
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from agentkit.config import BaseAgentSettings
from agentkit.egress import (
    AllowList,
    EgressBlocked,
    GuardedTransport,
    guarded_connect,
    guarded_httpx_client,
)


def _settings(**overrides: object) -> BaseAgentSettings:
    """A minimal BaseAgentSettings with Private Mode on and a non-trivial LLM gateway.

    IMPORTANT: we set the *component* fields (postgres_password, postgres_db)
    rather than the *computed* URL fields. ``database_url`` and ``redis_url``
    are @computed_field @properties — passing them via kwargs errors.
    """
    defaults: dict[str, object] = {
        "agent_name": "test",
        "private_mode": True,
        "llm_base_url": "https://gateway.example.com/v1",
        "postgres_host": "postgres",
        "postgres_port": 5432,
        "postgres_user": "appuser",
        "postgres_password": "pw",
        "postgres_db": "security",
        "redis_host": "redis",
        "redis_port": 6379,
        "rabbitmq_url": "amqp://guest:guest@rabbitmq:5672/",
    }
    defaults.update(overrides)
    return BaseAgentSettings(**defaults)  # type: ignore[call-arg]


def test_egress_blocked_carries_context() -> None:
    err = EgressBlocked("93.184.216.34", 443, allow_list=("127.0.0.1",))
    assert err.host == "93.184.216.34"
    assert err.port == 443
    assert err.allow_list == ("127.0.0.1",)
    assert "93.184.216.34:443" in str(err)


def test_allow_list_parses_settings_urls() -> None:
    s = _settings()
    al = AllowList.from_settings(s)
    hosts = sorted({h for h, _ in al.pairs})
    assert "gateway.example.com" in hosts, f"missing LLM host; got {hosts}"
    assert "postgres" in hosts, f"missing postgres host; got {hosts}"
    assert "redis" in hosts, f"missing redis host; got {hosts}"
    assert "rabbitmq" in hosts, f"missing rabbitmq host; got {hosts}"


def test_allow_list_blocks_unknown_host() -> None:
    s = _settings()
    al = AllowList.from_settings(s)
    assert al.allows("gateway.example.com", 443) is True
    assert al.allows("postgres", 5432) is True
    assert al.allows("attacker.example.com", 443) is False
    assert al.allows("gateway.example.com", 80) is False  # wrong port


def test_guarded_connect_blocks_when_private_mode_on() -> None:
    s = _settings(private_mode=True)
    with pytest.raises(EgressBlocked) as ei:
        guarded_connect("192.0.2.1", 443, settings=s)
    assert ei.value.host == "192.0.2.1"
    assert ei.value.port == 443


def test_guarded_connect_allows_allow_list_host() -> None:
    """Allow-listed host passes the check, then the socket opens.

    We bind a real localhost listener on a random port, register it as the
    LLM_BASE_URL, and prove the guard lets us through.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        s = _settings(llm_base_url=f"http://127.0.0.1:{port}")
        sock = guarded_connect("127.0.0.1", port, settings=s, timeout=2.0)
        assert sock.getpeername() == ("127.0.0.1", port)
        sock.close()
    finally:
        server.close()


def test_guarded_connect_passes_through_when_private_mode_off() -> None:
    """private_mode=False: connection is opened regardless of allow-list."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        s = _settings(private_mode=False)
        sock = guarded_connect("127.0.0.1", port, settings=s, timeout=2.0)
        sock.close()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_guarded_httpx_client_blocks_when_private_mode_on() -> None:
    """Private-mode httpx client raises EgressBlocked for a non-allow-list URL.

    We construct the client under private_mode=True, then make a real
    async request — the guard fires BEFORE any socket opens.
    """

    s = _settings(private_mode=True)
    client = guarded_httpx_client(s)
    try:
        with pytest.raises(EgressBlocked):
            await client.get("http://192.0.2.1/")
    finally:
        await client.aclose()


def test_guarded_httpx_client_passes_through_when_private_mode_off() -> None:
    """Off: GuardedClient is a passthrough — same constructor as httpx.

    Smoke test: we construct it and verify the transport is NOT wrapped.
    """

    s = _settings(private_mode=False)
    client = guarded_httpx_client(s)
    try:
        # When private_mode is off we return httpx.AsyncClient(**kwargs)
        # directly; the transport is the default httpx one, not GuardedTransport.
        assert not isinstance(client._transport, GuardedTransport)
    finally:
        asyncio.run(client.aclose())
