"""Single-key dotenv I/O for the ``DASHBOARD_AGENTS`` registry key.

This module is the registry-write contract (ADR-0009) in code. It owns **only**
the ``DASHBOARD_AGENTS`` key in ``apps/dashboard/.env``; it never touches any
other key (``JWT_SECRET``, ``LLM_*``, …) and never reformats unrelated lines.

The schema is imported, never re-declared: every write goes through
:class:`dashboard.settings.AgentConfig` and is round-tripped through
:func:`dashboard.registry.build_registry` before the ``.env`` is touched, so the
JSON the CLI writes is *exactly* what the dashboard will parse.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Import — never re-declare — the dashboard's config models (registry-write
# contract §2/§3). A CI grep bans re-declaring the agent-config schema here.
from dashboard.registry import AgentSpec, build_registry
from dashboard.settings import AgentConfig, Settings

# The single source of truth: this exact key in the dashboard's env file.
DASHBOARD_AGENTS_KEY = "DASHBOARD_AGENTS"

# Repo root is three levels up from this file:
#   apps/platform-cli/src/platform_cli/config.py -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[4]

# The one env file the CLI reads/writes. Kept in a single constant so a layout
# change moves only this line.
DASHBOARD_ENV_PATH = REPO_ROOT / "apps" / "dashboard" / ".env"


def read_dotenv_value(env_path: Path, key: str) -> str | None:
    """Return the raw value for ``key`` in ``env_path`` or ``None`` if absent.

    Parses a minimal ``KEY=VALUE`` dotenv: blank lines and ``#`` comments are
    ignored; surrounding single/double quotes on the value are stripped. The
    file not existing is treated the same as the key being absent.
    """
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return None


def write_dotenv_value(env_path: Path, key: str, value: str) -> None:
    """Set ``key=value`` in ``env_path``, leaving every other line untouched.

    Replaces the line for ``key`` in place (preserving ordering) or appends it
    if absent. The whole file is written via an atomic temp-file rename in the
    same directory so a crash mid-write can never leave a half-written ``.env``.
    The value is single-quoted (it is a JSON array) so the dashboard's dotenv
    loader keeps it as one token.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    new_line = f"{key}={_quote(value)}"

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    replaced = False
    out: list[str] = []
    for line in lines:
        name = line.strip().partition("=")[0].strip()
        if name == key and not replaced:
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)

    _atomic_write(env_path, "\n".join(out) + "\n")


def _quote(value: str) -> str:
    """Single-quote a JSON value for dotenv unless it is already quoted."""
    if value and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value
    return f"'{value}'"


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a temp file + atomic rename."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress_unlink(tmp_name):
            os.unlink(tmp_name)
        raise


class suppress_unlink:
    """Context manager that swallows errors while removing a temp file."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __enter__(self) -> suppress_unlink:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return True


def load_agents(env_path: Path = DASHBOARD_ENV_PATH) -> list[AgentConfig]:
    """Return the agents currently declared in ``DASHBOARD_AGENTS``.

    When the key is absent the registry is seeded from the live default
    (``Settings().agents`` == ``DEFAULT_AGENTS``) so an ``add`` never silently
    drops the base set (registry-write contract §4).
    """
    raw = read_dotenv_value(env_path, DASHBOARD_AGENTS_KEY)
    if raw is None:
        return list(Settings().agents)
    return [AgentConfig(**obj) for obj in json.loads(raw)]


def upsert(agents: list[AgentConfig], new: AgentConfig) -> list[AgentConfig]:
    """Replace any same-slug entry with ``new`` (appended last), else append.

    Order-preserving: existing entries keep their relative order; a brand-new
    slug lands at the end.
    """
    out = [a for a in agents if a.slug != new.slug]
    out.append(new)
    return out


def remove(agents: list[AgentConfig], slug: str) -> list[AgentConfig]:
    """Drop the entry with ``slug`` (order-preserving). Idempotent if absent."""
    return [a for a in agents if a.slug != slug]


def serialize(agents: list[AgentConfig]) -> str:
    """Render ``agents`` as the compact one-line JSON array the dashboard parses."""
    return json.dumps([a.model_dump() for a in agents], separators=(",", ":"))


def registry_for(agents: list[AgentConfig]) -> list[AgentSpec]:
    """Build the dashboard's ``AgentSpec`` view of ``agents``.

    The single chokepoint where the CLI constructs a dashboard ``Settings`` to
    drive ``build_registry`` — the same read path the dashboard uses, so the CLI
    sees identical specs (registry-write contract §3). A throwaway ``jwt_secret``
    is required by the base settings but is never persisted (the CLI owns only
    ``DASHBOARD_AGENTS``).
    """
    settings = Settings(jwt_secret="cli-validate", agents=[a.model_dump() for a in agents])  # noqa: S106
    return list(build_registry(settings))


def save_agents(env_path: Path, agents: list[AgentConfig]) -> None:
    """Validate ``agents`` through the dashboard's parser, then write the key.

    The payload is re-parsed through ``build_registry`` — the same path the
    dashboard uses — and the resulting slugs are asserted to match the input
    before the ``.env`` is touched. A validation failure raises before any
    write, so the ``.env`` is never left half-written.
    """
    payload = serialize(agents)
    registry = registry_for([AgentConfig(**o) for o in json.loads(payload)])
    if [s.slug for s in registry] != [a.slug for a in agents]:
        raise ValueError("registry round-trip mismatch — refusing to write")
    write_dotenv_value(env_path, DASHBOARD_AGENTS_KEY, payload)
