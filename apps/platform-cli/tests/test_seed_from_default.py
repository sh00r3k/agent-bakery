"""@spec US-013 — when DASHBOARD_AGENTS is absent, add seeds from DEFAULT_AGENTS (base set kept)."""

from __future__ import annotations

from pathlib import Path

import pytest
from dashboard.settings import DEFAULT_AGENTS
from platform_cli import config, registry_cmds


def test_load_agents_seeds_from_default_when_key_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No env file, no DASHBOARD_AGENTS in the process env -> live default.
    monkeypatch.delenv("DASHBOARD_AGENTS", raising=False)
    env_path = tmp_path / ".env"
    loaded = config.load_agents(env_path)
    assert [a.slug for a in loaded] == [a.slug for a in DEFAULT_AGENTS]


def test_add_to_absent_key_keeps_base_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_AGENTS", raising=False)
    env_path = tmp_path / ".env"
    rc = registry_cmds.agent_add(
        slug="invoices",
        url="http://invoices:8000",
        port=8006,
        features=["incidents"],
        env_path=env_path,
    )
    assert rc == 0
    after = config.load_agents(env_path)
    slugs = [a.slug for a in after]
    # The base set survived AND the new agent is appended last.
    for default in DEFAULT_AGENTS:
        assert default.slug in slugs
    assert slugs[-1] == "invoices"
