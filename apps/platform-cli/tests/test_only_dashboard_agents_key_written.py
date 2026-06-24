"""@spec BR-010 — other .env keys are byte-for-byte unchanged after a registry write."""

from __future__ import annotations

from pathlib import Path

import pytest
from platform_cli import config, registry_cmds

EXISTING_ENV = (
    "# dashboard env\n"
    "JWT_SECRET=super-secret-value\n"
    "LLM_API_KEY=sk-abc123\n"
    "LLM_MODEL=gpt-x\n"
    "DASHBOARD_BRAND='agents'\n"
)


def test_only_dashboard_agents_key_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_AGENTS", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(EXISTING_ENV, encoding="utf-8")

    rc = registry_cmds.agent_add(
        slug="invoices",
        url="http://invoices:8000",
        port=8006,
        features=["incidents"],
        env_path=env_path,
    )
    assert rc == 0

    after = env_path.read_text(encoding="utf-8")
    # Every original line is still present, unchanged.
    for line in EXISTING_ENV.splitlines():
        assert line in after.splitlines()
    # The new key was appended.
    assert config.read_dotenv_value(env_path, "DASHBOARD_AGENTS") is not None
    # The secrets were not duplicated or mangled.
    assert after.count("JWT_SECRET=super-secret-value") == 1
    assert config.read_dotenv_value(env_path, "JWT_SECRET") == "super-secret-value"
    assert config.read_dotenv_value(env_path, "LLM_API_KEY") == "sk-abc123"


def test_rewriting_existing_agents_key_preserves_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_AGENTS", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        EXISTING_ENV + 'DASHBOARD_AGENTS=\'[{"slug":"a","url":"http://a:8000"}]\'\n',
        encoding="utf-8",
    )
    before_secret = config.read_dotenv_value(env_path, "JWT_SECRET")

    rc = registry_cmds.agent_add(slug="b", url="http://b:8000", env_path=env_path)
    assert rc == 0

    assert config.read_dotenv_value(env_path, "JWT_SECRET") == before_secret
    # Only one DASHBOARD_AGENTS line — the key was replaced in place, not appended.
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in lines if line.startswith("DASHBOARD_AGENTS=")) == 1
