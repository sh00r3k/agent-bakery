"""Pytest fixtures + import shim for the standalone classifier-agent example.

examples/ is intentionally NOT a uv workspace member, so this package is never
pip-installed; the shim below puts the example directory on ``sys.path`` so
``import classifier_agent`` resolves during collection.

Why the fakes live here (a root-level, NON-package conftest) rather than in a
``tests/conftest.py``: a ``tests`` package would be imported as the module
``tests.conftest``, a name several suites in this monorepo already use — under
pytest's default prepend import-mode a bare repo-root ``pytest`` then aborts with
ImportPathMismatchError. Keeping this example's fakes in a uniquely-named
top-level ``conftest`` makes it a clean template you can copy without inheriting
that landmine.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from typing import Any

import pytest
from agentkit import Usage
from classifier_agent.schema import Classification


class FakeLLMClient:
    """LLMClient stand-in returning a canned Classification (or raising).

    Implements only ``complete_json`` (the method the graph calls); the real
    method's retry/cost discipline is covered by agentkit's own test_llm_json.py,
    so here we just drive the graph's success + fallback branches.
    """

    def __init__(
        self,
        *,
        result: Classification | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.result = result or Classification(label="bug", confidence=0.9, rationale="stack trace")
        self.raises = raises
        self.usage = Usage()
        self.calls: list[list[dict[str, str]]] = []

    async def complete_json(
        self, messages: list[dict[str, str]], *, schema: Any, **kwargs: Any
    ) -> tuple[Classification, Usage]:
        self.calls.append(messages)
        if self.raises is not None:
            raise self.raises
        return self.result, self.usage


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture
def make_fake_llm() -> Any:
    """Factory for FakeLLMClient instances with a custom result or error."""

    def _make(
        *, result: Classification | None = None, raises: BaseException | None = None
    ) -> FakeLLMClient:
        return FakeLLMClient(result=result, raises=raises)

    return _make
