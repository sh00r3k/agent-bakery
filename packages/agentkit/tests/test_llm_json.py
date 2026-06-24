"""@spec BR-006 — complete_json respects the same ceiling as complete().

Offline tests for LLMClient.complete_json: schema validation + bounded retry.

No network — the chat client is a fake returning a SEQUENCE of canned responses
(one per create() call) so the corrective-retry loop can be exercised. Cost,
breaker and usage discipline are inherited from complete() and proven here via
the call counter and the accumulated client.usage.
"""

from __future__ import annotations

from typing import Any

import pytest
from agentkit import llm as llm_mod
from pydantic import BaseModel

# Reference LLMClient/Usage/errors via llm_mod, never by direct import: a sibling
# test (test_llm_cost.py) reloads agentkit.llm, which rebinds these classes — a
# stale direct import would fail isinstance/identity checks once that test runs.


class Person(BaseModel):
    name: str
    age: int


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str, prompt_tokens: int = 10, completion_tokens: int = 5) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)


class _SeqCompletions:
    """Returns the next queued _FakeResponse per create(); records calls + last_kwargs."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


class _FakeChatNamespace:
    def __init__(self, completions: _SeqCompletions) -> None:
        self.completions = completions


class _FakeChatClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.completions = _SeqCompletions(responses)
        self.chat = _FakeChatNamespace(self.completions)


def _client(
    responses: list[_FakeResponse], *, max_cost_usd: float = 1.0, model: str = "minimax-m3"
) -> tuple[_FakeChatClient, llm_mod.LLMClient]:
    chat = _FakeChatClient(responses)
    client = llm_mod.LLMClient(
        chat=chat,  # type: ignore[arg-type]
        embed=object(),  # type: ignore[arg-type]
        model=model,
        embed_model="nomic-embed-text",
        max_cost_usd=max_cost_usd,
    )
    return chat, client


_MSGS = [{"role": "user", "content": "who is Ann"}]


async def test_valid_first_try() -> None:
    chat, client = _client([_FakeResponse('{"name":"Ann","age":30}', 100, 5)])
    obj, usage = await client.complete_json(_MSGS, schema=Person)
    assert obj == Person(name="Ann", age=30)
    assert isinstance(usage, llm_mod.Usage)
    assert chat.completions.calls == 1
    assert client.usage.prompt_tokens == 100


async def test_valid_first_try_fenced() -> None:
    chat, client = _client([_FakeResponse('```json\n{"name":"Ann","age":30}\n```')])
    obj, _ = await client.complete_json(_MSGS, schema=Person)
    assert obj == Person(name="Ann", age=30)
    assert chat.completions.calls == 1


async def test_invalid_then_valid() -> None:
    chat, client = _client([_FakeResponse("not json"), _FakeResponse('{"name":"Bo","age":5}')])
    obj, _ = await client.complete_json(_MSGS, schema=Person, max_retries=2)
    assert obj == Person(name="Bo", age=5)
    assert chat.completions.calls == 2
    sent = chat.completions.last_kwargs["messages"]
    # The failed assistant turn is fed back, then a corrective user message.
    assert {"role": "assistant", "content": "not json"} in sent
    corrective = sent[-1]["content"]
    assert "Return ONLY a JSON object" in corrective
    assert '"age"' in corrective  # the schema fragment is embedded


async def test_schema_mismatch_retries() -> None:
    # First reply is valid JSON but missing the required `age` -> ValidationError.
    chat, client = _client([_FakeResponse('{"name":"x"}'), _FakeResponse('{"name":"y","age":7}')])
    obj, _ = await client.complete_json(_MSGS, schema=Person, max_retries=2)
    assert obj == Person(name="y", age=7)
    assert chat.completions.calls == 2


async def test_empty_object_fails_required_fields() -> None:
    chat, client = _client([_FakeResponse("{}"), _FakeResponse('{"name":"z","age":1}')])
    obj, _ = await client.complete_json(_MSGS, schema=Person, max_retries=2)
    assert obj == Person(name="z", age=1)
    assert chat.completions.calls == 2


async def test_exhausted_raises() -> None:
    chat, client = _client([_FakeResponse("garbage")])
    with pytest.raises(llm_mod.JSONValidationError):
        await client.complete_json(_MSGS, schema=Person, max_retries=1)
    assert chat.completions.calls == 2  # == max_retries + 1


async def test_usage_accumulates_across_attempts() -> None:
    chat, client = _client(
        [_FakeResponse("not json", 100, 10), _FakeResponse('{"name":"Z","age":1}', 120, 20)]
    )
    _, usage = await client.complete_json(_MSGS, schema=Person, max_retries=2)
    assert usage.prompt_tokens == 220
    assert usage.completion_tokens == 30
    assert client.usage.prompt_tokens == 220
    assert chat.completions.calls == 2


async def test_cost_limit_not_retried_preflight() -> None:
    chat, client = _client([_FakeResponse('{"name":"a","age":1}')], max_cost_usd=0.0001)
    msgs = [{"role": "user", "content": "z" * 4_000_000}]
    with pytest.raises(llm_mod.CostLimitExceeded, match="pre-flight"):
        await client.complete_json(msgs, schema=Person)
    assert chat.completions.calls == 0  # propagates, never reaches the gateway


async def test_circuit_open_not_retried() -> None:
    chat = _FakeChatClient([_FakeResponse('{"name":"a","age":1}')])
    client = llm_mod.LLMClient(
        chat=chat,  # type: ignore[arg-type]
        embed=object(),  # type: ignore[arg-type]
        model="minimax-m3",
        embed_model="nomic-embed-text",
        breaker=llm_mod._CircuitBreaker(threshold=1, cooldown_s=30.0),
    )
    client.breaker.record_failure()  # trips OPEN (threshold == 1)
    with pytest.raises(llm_mod.CircuitOpenError):
        await client.complete_json(_MSGS, schema=Person)
    assert chat.completions.calls == 0
