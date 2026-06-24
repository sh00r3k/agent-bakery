"""@spec US-013 — write N agents, re-parse via build_registry, assert match."""

from __future__ import annotations

from pathlib import Path

from dashboard.registry import build_registry
from dashboard.settings import AgentConfig, Settings
from platform_cli import config


def test_config_roundtrip(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    agents = [
        AgentConfig(slug="security", url="http://security:8000", port=8000, features=["findings"]),
        AgentConfig(
            slug="monitoring", url="http://monitoring:8000", port=8002, features=["incidents"]
        ),
        AgentConfig(
            slug="batchy", url="http://batchy:8000", kind="batch", port=0, features=["runs"]
        ),
    ]
    config.save_agents(env_path, agents)

    # Re-read what the dashboard would parse from the same file.
    loaded = config.load_agents(env_path)
    registry = build_registry(
        Settings(jwt_secret="x", agents=[a.model_dump() for a in loaded])  # type: ignore[arg-type]
    )

    assert [s.slug for s in registry] == ["security", "monitoring", "batchy"]
    assert [s.kind for s in registry] == ["server", "server", "batch"]
    assert registry[0].has_findings is True
    assert registry[1].has_incidents is True
    assert registry[2].has_runs is True
