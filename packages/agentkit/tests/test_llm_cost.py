"""@spec BR-006, BR-007 — per-request USD ceiling + per-job cost cap.

Offline tests for the LLM cost meter: estimation, the pre-flight ceiling
guard (refuse BEFORE billing), the post-call backstop, and env-overridable
PRICES. No network — the chat client is a fake that records whether it was
called so we can prove the pre-flight guard short-circuits the request."""

from __future__ import annotations

import importlib

import pytest
from agentkit import llm as llm_mod
from agentkit.llm import (
    CostLimitExceeded,
    LLMClient,
    Usage,
    estimate_cost,
    estimate_max_cost,
    estimate_prompt_tokens,
)


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
    def __init__(self, content: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)


class _FakeCompletions:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self._response


class _FakeChatNamespace:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeChatClient:
    """Mimics AsyncOpenAI: exposes ``.chat.completions.create``."""

    def __init__(self, response: _FakeResponse) -> None:
        self.completions = _FakeCompletions(response)
        self.chat = _FakeChatNamespace(self.completions)


def _client(response: _FakeResponse, *, max_cost_usd: float = 0.10, model: str = "minimax-m3"):
    chat = _FakeChatClient(response)
    return chat, LLMClient(
        chat=chat,  # type: ignore[arg-type]
        embed=object(),  # type: ignore[arg-type] - unused here
        model=model,
        embed_model="nomic-embed-text",
        max_cost_usd=max_cost_usd,
    )


# --- estimation ------------------------------------------------------------


def test_estimate_cost_uses_price_table():
    # minimax-m3 = (0.3 in, 1.2 out) USD / Mtok
    cost = estimate_cost("minimax-m3", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.3 + 1.2)


def test_unknown_model_fails_closed_with_sentinel():
    # An unpriced model is billed at the high sentinel by default (fail CLOSED),
    # so the per-request ceiling still triggers on a runaway call.
    cost = estimate_cost("does-not-exist", 5_000_000, 5_000_000)
    sentinel_in, sentinel_out = llm_mod.UNPRICED_SENTINEL
    assert cost == pytest.approx((5_000_000 * sentinel_in + 5_000_000 * sentinel_out) / 1_000_000)
    assert cost > 0.0


def test_unknown_model_meters_zero_with_opt_in():
    # Explicit opt-in restores the old permissive metering-as-zero behavior.
    assert estimate_cost("does-not-exist", 5_000_000, 5_000_000, allow_unpriced=True) == 0.0


def test_estimate_max_cost_is_worst_case():
    # all prompt tokens billed + a full max_tokens completion
    assert estimate_max_cost("minimax-m3", 2_000_000, 1_000_000) == pytest.approx(
        estimate_cost("minimax-m3", 2_000_000, 1_000_000)
    )


def test_estimate_prompt_tokens_len_heuristic():
    msgs = [{"role": "user", "content": "x" * 400}]
    # ~4 chars/token -> ~100, plus per-message overhead
    assert estimate_prompt_tokens(msgs) >= 100


# --- post-call backstop ----------------------------------------------------


@pytest.mark.asyncio
async def test_post_call_backstop_raises_on_actual_spend():
    # Cheap prompt (passes pre-flight) but the gateway reports a huge spend.
    resp = _FakeResponse("ok", prompt_tokens=10_000_000, completion_tokens=10_000_000)
    chat, client = _client(resp, max_cost_usd=0.10)
    with pytest.raises(CostLimitExceeded):
        await client.complete([{"role": "user", "content": "hi"}])
    # the call DID happen (this is the backstop, not the pre-flight)
    assert chat.completions.calls == 1


@pytest.mark.asyncio
async def test_under_ceiling_returns_text_and_accumulates_usage():
    resp = _FakeResponse("hello", prompt_tokens=100, completion_tokens=50)
    _chat, client = _client(resp, max_cost_usd=1.0)
    text, usage = await client.complete([{"role": "user", "content": "hi"}])
    assert text == "hello"
    assert isinstance(usage, Usage)
    assert client.usage.prompt_tokens == 100
    assert client.usage.completion_tokens == 50


@pytest.mark.asyncio
async def test_usage_attributes_cost_per_model():
    resp = _FakeResponse("hi", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    _chat, client = _client(resp, max_cost_usd=10.0, model="minimax-m3")
    _text, usage = await client.complete([{"role": "user", "content": "hi"}])
    # single call: one model entry equal to its cost
    assert usage.by_model == {"minimax-m3": pytest.approx(0.3 + 1.2)}
    # accumulator mirrors it, and a second model accrues separately
    await client.complete([{"role": "user", "content": "hi"}], model="qwen3.5-flash")
    assert set(client.usage.by_model) == {"minimax-m3", "qwen3.5-flash"}
    assert client.usage.by_model["minimax-m3"] == pytest.approx(0.3 + 1.2)


def test_usage_add_merges_by_model():
    a = Usage(cost_usd=1.0, by_model={"m1": 1.0})
    a.add(Usage(cost_usd=2.0, by_model={"m1": 0.5, "m2": 1.5}))
    assert a.cost_usd == pytest.approx(3.0)
    assert a.by_model == {"m1": pytest.approx(1.5), "m2": pytest.approx(1.5)}


# --- pre-flight guard ------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_refuses_before_calling():
    # Huge prompt -> projected cost exceeds ceiling; must NOT hit the gateway.
    huge = "z" * 4_000_000  # ~1M input tokens
    resp = _FakeResponse("never", prompt_tokens=0, completion_tokens=0)
    chat, client = _client(resp, max_cost_usd=0.01)
    with pytest.raises(CostLimitExceeded, match="pre-flight"):
        await client.complete([{"role": "user", "content": huge}])
    assert chat.completions.calls == 0  # short-circuited before billing


@pytest.mark.asyncio
async def test_preflight_fails_closed_for_unknown_model():
    # An unpriced model now fails CLOSED: the pre-flight projects the sentinel
    # price, so a large call is refused BEFORE billing rather than slipping by.
    huge = "z" * 4_000_000
    resp = _FakeResponse("ok", prompt_tokens=1_000_000, completion_tokens=0)
    chat, client = _client(resp, max_cost_usd=0.0001, model="unpriced-model")
    with pytest.raises(CostLimitExceeded, match="pre-flight"):
        await client.complete([{"role": "user", "content": huge}])
    assert chat.completions.calls == 0  # short-circuited before billing


@pytest.mark.asyncio
async def test_unpriced_opt_in_restores_zero_metering():
    # With allow_unpriced_models the old behavior returns: an unknown model
    # meters $0, so the call proceeds and does not raise.
    huge = "z" * 4_000_000
    resp = _FakeResponse("ok", prompt_tokens=1_000_000, completion_tokens=0)
    chat = _FakeChatClient(resp)
    client = LLMClient(
        chat=chat,  # type: ignore[arg-type]
        embed=object(),  # type: ignore[arg-type]
        model="unpriced-model",
        embed_model="nomic-embed-text",
        max_cost_usd=0.0001,
        allow_unpriced_models=True,
    )
    text, _ = await client.complete([{"role": "user", "content": huge}])
    assert text == "ok"
    assert chat.completions.calls == 1


# --- env-overridable PRICES ------------------------------------------------


def test_prices_env_override(monkeypatch):
    monkeypatch.setenv("AGENTKIT_PRICES", '{"my-model": [2.0, 8.0]}')
    reloaded = importlib.reload(llm_mod)
    try:
        assert reloaded.PRICES["my-model"] == (2.0, 8.0)
        # seeded defaults still present (merge, not replace)
        assert reloaded.PRICES["minimax-m3"] == (0.3, 1.2)
        assert reloaded.estimate_cost("my-model", 1_000_000, 1_000_000) == pytest.approx(10.0)
    finally:
        monkeypatch.delenv("AGENTKIT_PRICES", raising=False)
        importlib.reload(llm_mod)


def test_bad_prices_override_is_ignored(monkeypatch):
    monkeypatch.setenv("AGENTKIT_PRICES", "{not json")
    reloaded = importlib.reload(llm_mod)
    try:
        # defaults survive a malformed override
        assert reloaded.PRICES["minimax-m3"] == (0.3, 1.2)
    finally:
        monkeypatch.delenv("AGENTKIT_PRICES", raising=False)
        importlib.reload(llm_mod)


# --- circuit breaker -------------------------------------------------------


class _FlakyCompletions:
    """Always raises, so consecutive calls can trip the breaker."""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        raise RuntimeError("gateway down")


class _FlakyChatClient:
    def __init__(self) -> None:
        self.completions = _FlakyCompletions()
        self.chat = _FakeChatNamespace(self.completions)


def test_breaker_disabled_when_threshold_zero():
    cb = llm_mod._CircuitBreaker(threshold=0, cooldown_s=30.0)
    # never opens regardless of failures
    for _ in range(10):
        cb.record_failure()
    cb.check()  # must not raise


def test_breaker_opens_after_threshold_then_half_opens():
    cb = llm_mod._CircuitBreaker(threshold=3, cooldown_s=30.0)
    cb.record_failure()
    cb.record_failure()
    cb.check()  # still closed (2 < 3)
    cb.record_failure()  # trips open
    with pytest.raises(llm_mod.CircuitOpenError):
        cb.check()
    # after cooldown elapses the breaker half-opens (allows one trial)
    cb._opened_at = cb._opened_at - 100.0  # type: ignore[operator]
    cb.check()  # no raise -> half-open trial permitted
    cb.record_success()  # trial succeeded -> fully closed
    cb.check()


@pytest.mark.asyncio
async def test_breaker_fails_fast_on_complete_after_threshold():
    chat = _FlakyChatClient()
    client = LLMClient(
        chat=chat,  # type: ignore[arg-type]
        embed=object(),  # type: ignore[arg-type]
        model="minimax-m3",
        embed_model="nomic-embed-text",
        max_cost_usd=1.0,
        breaker=llm_mod._CircuitBreaker(threshold=2, cooldown_s=30.0),
    )
    msgs = [{"role": "user", "content": "hi"}]
    # first two real attempts hit the (flaky) gateway and re-raise
    for _ in range(2):
        with pytest.raises(RuntimeError, match="gateway down"):
            await client.complete(msgs)
    assert chat.completions.calls == 2
    # breaker now OPEN -> next call fails fast without touching the gateway
    with pytest.raises(llm_mod.CircuitOpenError):
        await client.complete(msgs)
    assert chat.completions.calls == 2  # not incremented


class _RaisingCompletions:
    """Raises a caller-supplied exception each create() call (to drive the breaker)."""

    def __init__(self, exc: BaseException) -> None:
        self.calls = 0
        self._exc = exc

    async def create(self, **kwargs):
        self.calls += 1
        raise self._exc


class _RaisingChatClient:
    def __init__(self, exc: BaseException) -> None:
        self.completions = _RaisingCompletions(exc)
        self.chat = _FakeChatNamespace(self.completions)


def _breaker_client(chat, *, threshold: int = 3):
    return LLMClient(
        chat=chat,  # type: ignore[arg-type]
        embed=object(),  # type: ignore[arg-type]
        model="minimax-m3",
        embed_model="nomic-embed-text",
        max_cost_usd=1.0,
        breaker=llm_mod._CircuitBreaker(threshold=threshold, cooldown_s=30.0),
    )


def _openai_4xx() -> BaseException:
    """Construct an openai 4xx (BadRequestError) the way the SDK does at runtime."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://gw.example/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return openai.BadRequestError("bad input", response=response, body=None)


@pytest.mark.asyncio
async def test_client_4xx_does_not_advance_breaker():
    # A client-side 4xx (one tenant's bad input) must NOT count toward opening the
    # shared per-process breaker — otherwise it would fail-fast every other tenant.
    chat = _RaisingChatClient(_openai_4xx())
    client = _breaker_client(chat, threshold=3)
    msgs = [{"role": "user", "content": "hi"}]
    import openai

    for _ in range(5):
        with pytest.raises(openai.BadRequestError):
            await client.complete(msgs)
    assert chat.completions.calls == 5  # every call reached the gateway
    assert client.breaker._failures == 0  # breaker untouched by 4xx
    client.breaker.check()  # still CLOSED -> no raise


@pytest.mark.asyncio
async def test_client_cost_limit_does_not_advance_breaker():
    # The post-call cost-ceiling backstop raises CostLimitExceeded; it is a 4xx-
    # equivalent client outcome and must not advance the breaker either.
    resp = _FakeResponse("ok", prompt_tokens=10_000_000, completion_tokens=10_000_000)
    chat = _FakeChatClient(resp)
    client = _breaker_client(chat, threshold=2)
    msgs = [{"role": "user", "content": "hi"}]
    # Reference the class via the module: the prices tests reload llm_mod, which
    # rebinds CostLimitExceeded — using the module attribute stays correct
    # regardless of test ordering.
    for _ in range(3):
        with pytest.raises(llm_mod.CostLimitExceeded):
            await client.complete(msgs)
    # the gateway call succeeded each time (cost check is post-call) -> breaker
    # saw successes, never a failure.
    assert client.breaker._failures == 0
    client.breaker.check()


@pytest.mark.asyncio
async def test_client_transport_error_advances_breaker():
    # A genuine transport failure (connection error) DOES advance the breaker and
    # opens it at the threshold.
    import httpx
    import openai

    request = httpx.Request("POST", "https://gw.example/v1/chat/completions")
    transport_exc = openai.APIConnectionError(request=request)
    chat = _RaisingChatClient(transport_exc)
    client = _breaker_client(chat, threshold=2)
    msgs = [{"role": "user", "content": "hi"}]
    for _ in range(2):
        with pytest.raises(openai.APIConnectionError):
            await client.complete(msgs)
    assert chat.completions.calls == 2
    # breaker now OPEN -> next call fails fast without touching the gateway
    with pytest.raises(llm_mod.CircuitOpenError):
        await client.complete(msgs)
    assert chat.completions.calls == 2


@pytest.mark.asyncio
async def test_client_5xx_advances_breaker():
    # A 5xx server/gateway error counts toward the breaker (it is a gateway fault).
    import httpx
    import openai

    request = httpx.Request("POST", "https://gw.example/v1/chat/completions")
    response = httpx.Response(503, request=request)
    server_exc = openai.InternalServerError("upstream down", response=response, body=None)
    chat = _RaisingChatClient(server_exc)
    client = _breaker_client(chat, threshold=2)
    msgs = [{"role": "user", "content": "hi"}]
    for _ in range(2):
        with pytest.raises(openai.InternalServerError):
            await client.complete(msgs)
    with pytest.raises(llm_mod.CircuitOpenError):
        await client.complete(msgs)
    assert chat.completions.calls == 2


def test_client_breaker_disabled_by_default():
    # A directly-constructed client keeps the old fail-on-every-call shape.
    client = LLMClient(
        chat=object(),  # type: ignore[arg-type]
        embed=object(),  # type: ignore[arg-type]
        model="minimax-m3",
        embed_model="nomic-embed-text",
    )
    assert client.breaker.threshold == 0
