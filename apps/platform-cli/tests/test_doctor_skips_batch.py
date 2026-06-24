"""@spec US-013 — batch/port==0 agents are not probed; unreachable is reported, not raised."""

from __future__ import annotations

import httpx
import pytest
from dashboard.registry import AgentSpec
from platform_cli import doctor


def _spec(slug: str, *, kind: str = "server", port: int = 8000) -> AgentSpec:
    return AgentSpec(slug=slug, title=slug, base_url=f"http://{slug}:8000", port=port, kind=kind)  # type: ignore[arg-type]


def test_should_skip_batch_and_portless() -> None:
    assert doctor._should_skip(_spec("b", kind="batch", port=0)) is True
    assert doctor._should_skip(_spec("p", kind="server", port=0)) is True
    assert doctor._should_skip(_spec("ok", kind="server", port=8000)) is False


async def test_probe_skips_batch_and_reports_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = [
        _spec("healthy"),
        _spec("down"),
        _spec("batchy", kind="batch", port=0),
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "healthy":
            return httpx.Response(200, json={"ok": True})
        # "down" host: simulate an unreachable agent.
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(_handler)

    real_client = httpx.AsyncClient

    def _patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return real_client(transport=transport)

    monkeypatch.setattr(httpx, "AsyncClient", _patched_client)

    results = await doctor.probe(specs)
    by_slug = {r.slug: r for r in results}

    # batch agent skipped, never probed.
    assert by_slug["batchy"].skipped is True
    assert by_slug["batchy"].healthz_ok is None

    # healthy probed OK.
    assert by_slug["healthy"].skipped is False
    assert by_slug["healthy"].healthz_ok is True
    assert by_slug["healthy"].readyz_ok is True

    # unreachable reported (not raised).
    assert by_slug["down"].healthz_ok is False
    assert by_slug["down"].error is not None
