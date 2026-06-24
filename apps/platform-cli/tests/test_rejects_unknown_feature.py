"""@spec US-013 — --feature bogus rejected at the CLI; --kind bogus rejected by AgentConfig."""

from __future__ import annotations

from pathlib import Path

import pytest
from platform_cli import registry_cmds
from pydantic import ValidationError


def test_rejects_unknown_feature(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    with pytest.raises(ValueError, match="unknown feature"):
        registry_cmds.agent_add(
            slug="x",
            url="http://x:8000",
            features=["bogus"],
            env_path=env_path,
        )
    # Nothing was written.
    assert not env_path.exists()


def test_normalizes_known_feature_case_and_space() -> None:
    assert registry_cmds._normalize_features(["  Findings ", "INCIDENTS"]) == [
        "findings",
        "incidents",
    ]


def test_rejects_unknown_kind(tmp_path: Path) -> None:
    # Parity with dashboard's test_agent_config_rejects_unknown_kind:
    # AgentConfig validation raises on a bad Literal kind.
    env_path = tmp_path / ".env"
    with pytest.raises(ValidationError):
        registry_cmds.agent_add(
            slug="x",
            url="http://x:8000",
            kind="bogus",
            env_path=env_path,
        )
    assert not env_path.exists()
