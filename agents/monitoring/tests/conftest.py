"""Shared test fakes: an in-memory IncidentStore and a fake LLMClient.

These let the triage graph and dedup logic be tested without a live Postgres,
RabbitMQ, or LLM gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from agentkit import Usage
from monitoring_agent.store import Incident, make_dedup_key


class FakeIncidentStore:
    """In-memory stand-in for IncidentStore with the same dedup semantics.

    Mirrors the ON CONFLICT upsert: same dedup_key bumps count + last_seen and
    reports seconds_since_prev; a new key inserts with is_new=True.
    """

    def __init__(self) -> None:
        self._rows: dict[str, Incident] = {}
        self._next_id = 1
        # Allow tests to control "now" for deterministic window math.
        self.now = datetime.now(UTC)

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)

    async def init_schema(self) -> None:  # pragma: no cover - no-op
        pass

    async def upsert(self, *, dedup_key, source, severity, title, body) -> Incident:
        existing = self._rows.get(dedup_key)
        if existing is None:
            inc = Incident(
                id=self._next_id,
                dedup_key=dedup_key,
                source=source,
                severity=severity,
                title=title,
                body=body,
                count=1,
                first_seen=self.now,
                last_seen=self.now,
                status="open",
                is_new=True,
                seconds_since_prev=None,
            )
            self._next_id += 1
            self._rows[dedup_key] = inc
            return inc

        prev_last = existing.last_seen
        existing.count += 1
        existing.last_seen = self.now
        existing.severity = severity
        existing.title = title
        existing.body = body
        existing.is_new = False
        existing.seconds_since_prev = (self.now - prev_last).total_seconds()
        return existing

    async def recent(self, *, limit=50) -> list[Incident]:
        rows = sorted(self._rows.values(), key=lambda i: i.last_seen, reverse=True)
        return rows[:limit]

    async def mark_alert_failed(self, dedup_key) -> None:
        inc = self._rows.get(dedup_key)
        if inc is not None:
            inc.status = "alert_failed"

    async def clear_alert_failed(self, dedup_key) -> None:
        inc = self._rows.get(dedup_key)
        if inc is not None and inc.status == "alert_failed":
            inc.status = "open"

    async def prune(self, *, retention_days) -> int:  # pragma: no cover - exercised live
        return 0


class FakeLLMClient:
    """LLMClient stand-in returning a canned classification JSON."""

    def __init__(
        self,
        response: str = '{"severity":"critical","category":"outage","hypothesis":"upstream down"}',
    ) -> None:
        self.response = response
        self.usage = Usage()
        self.calls: list[list[dict]] = []

    async def complete(self, messages, *, model=None, max_tokens=None, temperature=0.2, **kwargs):
        self.calls.append(messages)
        return self.response, Usage(prompt_tokens=10, completion_tokens=5, cost_usd=0.0)


@pytest.fixture
def fake_store() -> FakeIncidentStore:
    return FakeIncidentStore()


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def dedup_key_factory():
    return make_dedup_key
