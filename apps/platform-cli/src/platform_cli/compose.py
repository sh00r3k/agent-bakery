"""``platform up`` / ``platform down`` — the docker-compose lifecycle.

Shells **plain** ``docker compose`` (no ``-f``) from the repo root, so the
default ``docker-compose.yml`` is used. ``--profile`` is a pass-through to the
compose's named profiles (``edge`` = Caddy TLS, ``meta`` = docker-socket-proxy);
bare ``platform up`` brings up **core only**.

Per ADR-0001 this must stay OSS docker compose — never a managed / LangGraph
Platform runner. No SDK, just a subprocess.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from .config import REPO_ROOT

# The named compose profiles the operator may opt into (must match the
# docker-compose.yml ``profiles:`` stanzas).
KNOWN_PROFILES: tuple[str, ...] = ("edge", "meta")


def _compose_argv(action: str, services: Sequence[str], profiles: Sequence[str]) -> list[str]:
    """Build the ``docker compose [--profile p …] <action> [svc …]`` argv.

    Profiles come before the subcommand (docker compose treats ``--profile`` as a
    top-level flag). ``up`` runs detached (``-d``); ``down`` keeps volumes.
    """
    argv = ["docker", "compose"]
    for profile in profiles:
        argv += ["--profile", profile]
    if action == "up":
        argv += ["up", "-d"]
    elif action == "down":
        argv += ["down"]
    else:  # pragma: no cover - argparse constrains the action
        raise ValueError(f"unknown compose action: {action}")
    argv += list(services)
    return argv


def compose(
    action: str,
    services: Sequence[str] = (),
    profiles: Sequence[str] = (),
) -> int:
    """Run ``docker compose`` from the repo root; return its exit code.

    Validates ``profiles`` against :data:`KNOWN_PROFILES` (fail-fast) before
    spawning, so a typo'd profile is rejected by the CLI rather than silently
    doing nothing.
    """
    bad = [p for p in profiles if p not in KNOWN_PROFILES]
    if bad:
        raise ValueError(f"unknown profile(s) {bad}; choose from {', '.join(KNOWN_PROFILES)}")
    argv = _compose_argv(action, services, profiles)
    # Fixed `docker compose` argv from validated profiles + pass-through service
    # names; no shell. Per ADR-0001 this stays plain OSS docker compose.
    return subprocess.run(argv, cwd=str(REPO_ROOT), check=False).returncode  # noqa: S603


def cmd_up(services: Sequence[str], profiles: Sequence[str]) -> int:
    return compose("up", services, profiles)


def cmd_down(services: Sequence[str], profiles: Sequence[str]) -> int:
    return compose("down", services, profiles)
