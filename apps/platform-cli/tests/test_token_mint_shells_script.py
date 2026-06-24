"""@spec US-013, BR-010 — token mint shells mint-admin-token.py with the right flags."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from platform_cli import token_cmds


def test_token_mint_shells_script_with_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Completed:
        returncode = 0

    def _fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    rc = token_cmds.token_mint(sub="alice", role="manager", ttl=120, audience="dash")
    assert rc == 0

    argv = captured["argv"]
    assert str(token_cmds.MINT_SCRIPT) in argv
    assert argv[argv.index("--sub") + 1] == "alice"
    assert argv[argv.index("--role") + 1] == "manager"
    assert argv[argv.index("--ttl") + 1] == "120"
    assert argv[argv.index("--audience") + 1] == "dash"


def test_token_mint_no_audience_omits_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Completed:
        returncode = 0

    def _fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    token_cmds.token_mint()
    assert "--audience" not in captured["argv"]


def test_token_mint_no_secret_exits_nonzero_and_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Real subprocess against the real script with NO JWT_SECRET: it must exit
    # non-zero and print nothing to stdout (the error goes to stderr).
    monkeypatch.delenv("JWT_SECRET", raising=False)
    rc = token_cmds.token_mint(sub="op", role="admin", ttl=60)
    assert rc != 0
    out = capsys.readouterr()
    assert out.out == ""
