"""@spec US-013 — adding an existing slug replaces (not duplicates);
order preserved; remove drops one.
"""

from __future__ import annotations

from dashboard.settings import AgentConfig
from platform_cli import config


def _agents() -> list[AgentConfig]:
    return [
        AgentConfig(slug="a", url="http://a:8000"),
        AgentConfig(slug="b", url="http://b:8000"),
        AgentConfig(slug="c", url="http://c:8000"),
    ]


def test_upsert_replaces_existing_slug() -> None:
    out = config.upsert(_agents(), AgentConfig(slug="b", url="http://new-b:9000", port=42))
    slugs = [a.slug for a in out]
    assert slugs.count("b") == 1
    # Replaced entry moves to the end (replace-by-slug then append).
    assert slugs == ["a", "c", "b"]
    replaced = next(a for a in out if a.slug == "b")
    assert replaced.url == "http://new-b:9000"
    assert replaced.port == 42


def test_upsert_appends_new_slug() -> None:
    out = config.upsert(_agents(), AgentConfig(slug="d", url="http://d:8000"))
    assert [a.slug for a in out] == ["a", "b", "c", "d"]


def test_remove_drops_exactly_one() -> None:
    out = config.remove(_agents(), "b")
    assert [a.slug for a in out] == ["a", "c"]


def test_remove_absent_is_noop() -> None:
    out = config.remove(_agents(), "zzz")
    assert [a.slug for a in out] == ["a", "b", "c"]
