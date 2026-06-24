"""Settings for classifier-agent: the closed label set + classify knobs."""

from __future__ import annotations

from functools import lru_cache

from agentkit import BaseAgentSettings
from pydantic import Field


# agentkit ships no py.typed stubs, so BaseAgentSettings resolves to Any and
# mypy --strict flags subclassing it; the gap is upstream, not in this file.
class Settings(BaseAgentSettings):  # type: ignore[misc]  # untyped agentkit base
    agent_name: str = "classifier-agent"
    port: int = 8000
    # Closed label set the classifier may emit; anything else => fallback_label.
    labels: list[str] = Field(
        default_factory=lambda: [
            "billing",
            "bug",
            "feature_request",
            "praise",
            "spam",
            "other",
        ]
    )
    # Deterministic fallback when the LLM fails or returns an off-set label.
    fallback_label: str = "other"
    # Output-token cap for the classify call (the JSON result is tiny).
    classify_max_tokens: int = 128
    # Hard cap on the untrusted input text fed to the model (AF-08).
    input_max_chars: int = 4000


@lru_cache
def get_settings() -> Settings:
    return Settings()
