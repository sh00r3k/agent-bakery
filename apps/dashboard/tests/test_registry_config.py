"""@spec US-013 — registry is fully env-driven; arbitrary composition works.

The agents registry is FULLY config/env-driven.

The dashboard makes no assumptions about which agents exist: the composition
comes entirely from ``settings.agents`` (a list of ``{slug,url,kind,features}``
sourced from the ``DASHBOARD_AGENTS`` env var or the in-process default). These
tests prove an arbitrary composition works end to end — including a single
custom agent, an empty agents, and capability-based resolution — with zero
hardcoded slugs in the rendering path.
"""

from __future__ import annotations

import json

import pytest
from dashboard import views
from dashboard.registry import build_registry, by_slug, with_feature
from dashboard.settings import AgentConfig, Settings

from .conftest import make_aggregator
from .test_aggregator import _agents_handler


def _settings(agents: list[dict]) -> Settings:
    return Settings(jwt_secret="test-secret-please-ignore", agents=agents)


def test_registry_built_from_arbitrary_config():
    s = _settings(
        [
            {
                "slug": "alpha",
                "url": "http://alpha:8000",
                "kind": "server",
                "features": ["incidents"],
            },
            {"slug": "beta", "url": "http://beta:9000", "kind": "batch", "features": ["runs"]},
        ]
    )
    reg = build_registry(s)
    assert [a.slug for a in reg] == ["alpha", "beta"]
    # title defaults to slug, kind/features mapped onto the spec.
    assert by_slug(reg, "alpha").title == "alpha"
    assert by_slug(reg, "alpha").has_incidents is True
    assert by_slug(reg, "beta").kind == "batch"
    assert by_slug(reg, "beta").has_runs is True
    # Registry is purely config-driven: no hardcoded agent slugs leak in.
    assert by_slug(reg, "gamma") is None
    assert by_slug(reg, "monitoring") is None


def test_registry_can_be_empty():
    reg = build_registry(_settings([]))
    assert reg == []
    assert with_feature(reg, "incidents") is None


def test_capability_resolution_is_slug_agnostic():
    # An agent named "ops-bot" declares the incidents feature; the dashboard must
    # resolve it as the incidents-capable agent by capability, not by slug.
    s = _settings([{"slug": "ops-bot", "url": "http://ops-bot:8000", "features": ["incidents"]}])
    reg = build_registry(s)
    hd = with_feature(reg, "incidents")
    assert hd is not None and hd.slug == "ops-bot"


def test_qa_resolves_by_coverage_capability_not_slug():
    # The QA panel must resolve its provider by the ``coverage`` capability, not
    # by a literal "ultraqa" slug. An agent named "tester" (NOT "ultraqa") that
    # declares ``coverage`` is the QA agent; a plain ``findings`` agent is not.
    s = _settings(
        [
            {"slug": "sec", "url": "http://sec:8000", "features": ["findings"]},
            {"slug": "tester", "url": "http://tester:8000", "features": ["findings", "coverage"]},
        ]
    )
    reg = build_registry(s)
    assert by_slug(reg, "tester").has_coverage is True
    assert by_slug(reg, "sec").has_coverage is False
    # findings (security) and coverage (QA) resolve to DISTINCT agents.
    assert with_feature(reg, "findings").slug == "sec"
    assert with_feature(reg, "coverage").slug == "tester"


@pytest.mark.asyncio
async def test_qa_context_degrades_without_coverage_agent():
    # No coverage-capable agent → the QA panel degrades gracefully (no slug crash).
    s = _settings([{"slug": "sec", "url": "http://sec:8000", "features": ["findings"]}])
    reg = build_registry(s)
    agg = make_aggregator(s, _agents_handler({"sec": {"healthz": True, "ready": True}}))
    qa = await views.qa_context(agg, reg)
    assert qa["unavailable"] == "no coverage-capable agent in registry"
    await agg.aclose()


def test_agents_parsed_from_env_json(monkeypatch):
    monkeypatch.setenv(
        "DASHBOARD_AGENTS",
        json.dumps([{"slug": "watcher", "url": "http://watcher:8000", "features": ["incidents"]}]),
    )
    s = Settings(jwt_secret="test-secret-please-ignore")
    reg = build_registry(s)
    assert [a.slug for a in reg] == ["watcher"]
    assert with_feature(reg, "incidents").slug == "watcher"


@pytest.mark.asyncio
async def test_views_degrade_gracefully_for_missing_capabilities():
    # A agents with ONLY a custom monitoring-like agent: incidents resolve to it;
    # findings/pm/runs have no provider and degrade (no crash).
    s = _settings(
        [{"slug": "ops", "url": "http://ops:8000", "kind": "server", "features": ["incidents"]}]
    )
    reg = build_registry(s)
    agg = make_aggregator(s, _agents_handler({"ops": {"healthz": True, "ready": True}}))

    inc = await views.incidents_context(agg, reg)
    # incidents-capable agent present (reachable) → no "not in registry" message.
    assert inc["unavailable"] != "no incidents-capable agent in registry"

    fnd = await views.findings_context(agg, reg)
    assert fnd["unavailable"] == "no findings-capable agent in registry"
    pm = await views.pm_context(agg, reg)
    assert pm["unavailable"] == "no pm-capable agent in registry"
    pipe = await views.pipeline_context(agg, reg)
    assert pipe["unavailable"] == "no runs-capable agent in registry"
    await agg.aclose()


@pytest.mark.asyncio
async def test_overview_renders_only_declared_capabilities():
    # Tile footer links/actions are capability-driven; a pm-only agent shows the
    # "digests →" link but NOT sweep/scan/incidents/findings buttons.
    s = _settings([{"slug": "pm-bot", "url": "http://pm-bot:8000", "features": ["pm"]}])
    reg = build_registry(s)
    agg = make_aggregator(s, _agents_handler({"pm-bot": {"healthz": True, "ready": True}}))
    agents = await agg.agents_health(reg)
    only = agents[0]
    assert only.has_pm is True
    assert only.has_incidents is False and only.has_findings is False
    await agg.aclose()


def test_agent_config_rejects_unknown_kind():
    with pytest.raises(Exception):
        AgentConfig(slug="x", url="http://x", kind="bogus")
