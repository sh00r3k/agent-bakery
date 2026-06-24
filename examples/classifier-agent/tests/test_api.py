"""@spec US-012, BR-006 — /classify happy + fallback, inherited /healthz.

API surface: /classify happy + fallback, and the inherited /healthz.

LLMClient.from_settings is monkeypatched to a FakeLLMClient (see the example's
root conftest.py) so the lifespan builds the graph against the fake — no network.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from agentkit import LLMClient
from asgi_lifespan import LifespanManager
from classifier_agent import api as api_mod
from fastapi import FastAPI


async def _post_classify(app: FastAPI, text: str) -> httpx.Response:
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        return await client.post("/classify", json={"text": text})


def _patch(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr(LLMClient, "from_settings", classmethod(lambda cls, settings: fake))


async def test_classify_endpoint_happy(monkeypatch: pytest.MonkeyPatch, make_fake_llm: Any) -> None:
    _patch(monkeypatch, make_fake_llm())
    resp = await _post_classify(api_mod.app, "crash on save")
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "bug"
    assert body["fell_back"] is False


async def test_classify_endpoint_fallback(
    monkeypatch: pytest.MonkeyPatch, make_fake_llm: Any
) -> None:
    _patch(monkeypatch, make_fake_llm(raises=RuntimeError("down")))
    resp = await _post_classify(api_mod.app, "anything")
    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "other"
    assert body["fell_back"] is True


async def test_healthz_is_free(monkeypatch: pytest.MonkeyPatch, make_fake_llm: Any) -> None:
    _patch(monkeypatch, make_fake_llm())
    async with (
        LifespanManager(api_mod.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_mod.app), base_url="http://t"
        ) as client,
    ):
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
