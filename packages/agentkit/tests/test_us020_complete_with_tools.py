"""@spec US-020, BR-006 — tool-calling on the shared LLM seam.

Offline tests that ``LLMClient.complete_with_tools`` surfaces the
``message.tool_calls`` + ``finish_reason`` that ``complete()`` drops, while
keeping the exact same pre-flight + post-call USD cost guards. No network — the
chat client is a fake recording the kwargs it received.
"""

from __future__ import annotations

import pytest
from agentkit import llm as llm_mod

# Reference all llm classes via the live module (not top-level imports): a
# sibling test reloads agentkit.llm, rebinding LLMClient/ToolTurn/ToolCall/Usage/
# CostLimitExceeded to NEW class objects. A captured-at-import reference would no
# longer match what complete_with_tools constructs or raises, so we go through
# ``llm_mod`` everywhere identity matters.


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content: str | None, tool_calls: list[_FakeToolCall] | None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage, finish_reason: str) -> None:
        self.message = message
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, choice: _FakeChoice, prompt_tokens: int, completion_tokens: int) -> None:
        self.choices = [choice]
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)


class _FakeCompletions:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls = 0
        self.last_kwargs: dict = {}

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self._response


class _FakeChatNamespace:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeChatClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.completions = _FakeCompletions(response)
        self.chat = _FakeChatNamespace(self.completions)


def _client(response: _FakeResponse, *, max_cost_usd: float = 1.0, model: str = "minimax-m3"):
    chat = _FakeChatClient(response)
    return chat, llm_mod.LLMClient(
        chat=chat,  # type: ignore[arg-type]
        embed=object(),  # type: ignore[arg-type]
        model=model,
        embed_model="nomic-embed-text",
        max_cost_usd=max_cost_usd,
    )


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "open a URL",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    }
]


def _tool_response(prompt_tokens: int = 100, completion_tokens: int = 20) -> _FakeResponse:
    msg = _FakeMessage(
        content=None,
        tool_calls=[_FakeToolCall("call_1", "navigate", '{"url": "http://frontend.local/"}')],
    )
    return _FakeResponse(_FakeChoice(msg, "tool_calls"), prompt_tokens, completion_tokens)


@pytest.mark.asyncio
async def test_surfaces_tool_calls_and_finish_reason():
    _chat, client = _client(_tool_response())
    turn, usage = await client.complete_with_tools(
        [{"role": "user", "content": "explore the app"}], tools=_TOOLS
    )
    assert isinstance(turn, llm_mod.ToolTurn)
    assert turn.content is None  # tool-call turns usually have no text
    assert turn.finish_reason == "tool_calls"
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert isinstance(call, llm_mod.ToolCall)
    assert call.id == "call_1"
    assert call.name == "navigate"
    assert call.arguments == '{"url": "http://frontend.local/"}'
    assert isinstance(usage, llm_mod.Usage)
    assert client.usage.prompt_tokens == 100


@pytest.mark.asyncio
async def test_forwards_tools_and_tool_choice_to_gateway():
    chat, client = _client(_tool_response())
    await client.complete_with_tools(
        [{"role": "user", "content": "hi"}], tools=_TOOLS, tool_choice="required"
    )
    kw = chat.completions.last_kwargs
    assert kw["tools"] == _TOOLS
    assert kw["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_no_tool_calls_returns_empty_list_and_text():
    msg = _FakeMessage(content="all good", tool_calls=None)
    resp = _FakeResponse(_FakeChoice(msg, "stop"), 10, 5)
    _chat, client = _client(resp)
    turn, _usage = await client.complete_with_tools(
        [{"role": "user", "content": "done?"}], tools=_TOOLS
    )
    assert turn.tool_calls == []
    assert turn.content == "all good"
    assert turn.finish_reason == "stop"


@pytest.mark.asyncio
async def test_preflight_ceiling_still_applies_before_calling():
    huge = "z" * 4_000_000  # ~1M input tokens -> projected cost over the ceiling
    chat, client = _client(_tool_response(), max_cost_usd=0.01)
    with pytest.raises(llm_mod.CostLimitExceeded, match="pre-flight"):
        await client.complete_with_tools([{"role": "user", "content": huge}], tools=_TOOLS)
    assert chat.completions.calls == 0  # short-circuited before billing


@pytest.mark.asyncio
async def test_post_call_backstop_still_applies():
    resp = _tool_response(prompt_tokens=10_000_000, completion_tokens=10_000_000)
    chat, client = _client(resp, max_cost_usd=0.10)
    with pytest.raises(llm_mod.CostLimitExceeded):
        await client.complete_with_tools([{"role": "user", "content": "hi"}], tools=_TOOLS)
    assert chat.completions.calls == 1  # the backstop, not the pre-flight


@pytest.mark.asyncio
async def test_handles_none_content_in_preflight_estimate():
    # A prior assistant tool-call message carries content=None; the loose
    # estimator must not crash on it.
    _chat, client = _client(_tool_response())
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None},
        {"role": "tool", "content": "result"},
    ]
    turn, _usage = await client.complete_with_tools(messages, tools=_TOOLS)
    assert turn.finish_reason == "tool_calls"
