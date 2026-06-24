"""Config-driven registry of the agents agents the dashboard observes.

The dashboard makes **no assumptions** about which agents exist. The agents
composition is declared entirely in configuration (``settings.agents``, see
:mod:`dashboard.settings`) — a list of ``{slug, url, kind, ...}`` entries
sourced from the ``DASHBOARD_AGENTS`` env var (JSON) or an in-process default.
Add, remove, or reorder agents purely by editing that list; nothing here is
hardcoded to a particular agent.

Each entry maps to an :class:`AgentSpec` with:

- ``kind`` — ``server`` (always-on HTTP), ``batch`` (no port / heartbeat-only),
  or ``self`` (the dashboard itself).
- feature flags (``has_incidents`` / ``has_findings`` / ``has_coverage`` /
  ``has_runs`` / ``has_pm``) — what the agent exposes, so the
  overview tiles and panels render *per declared capability* rather than per
  hardcoded slug. ``coverage`` marks a QA-style tester (its own findings feed +
  a ``/coverage`` rollup), distinguishing it from a plain security ``findings``
  agent without keying off a literal slug.

Kept declarative so the overview, nav, and aggregation layer all iterate one
list instead of per-agent special-casing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .settings import AgentConfig, Settings

AgentKind = Literal["server", "batch", "self"]

# Recognized capability flags an agent may declare via its ``features`` list.
# Each maps onto a ``has_*`` attribute the templates/views key off of.
KNOWN_FEATURES: tuple[str, ...] = (
    "incidents",
    "findings",
    "coverage",
    "runs",
    "pm",
)


@dataclass(frozen=True)
class AgentSpec:
    slug: str
    title: str
    base_url: str
    port: int  # host loopback port; 0 for batch (no port) / unset
    kind: AgentKind
    # Capabilities the agent exposes that the dashboard renders.
    has_incidents: bool = False
    has_findings: bool = False
    # QA-style tester: a distinct findings feed plus a /coverage rollup. Kept
    # separate from has_findings so the QA panel resolves by capability, not slug.
    has_coverage: bool = False
    has_runs: bool = False
    has_pm: bool = False


def _spec_from_config(cfg: AgentConfig) -> AgentSpec:
    features = {f.strip().lower() for f in (cfg.features or [])}
    return AgentSpec(
        slug=cfg.slug,
        title=cfg.title or cfg.slug,
        base_url=cfg.url,
        port=cfg.port,
        kind=cfg.kind,  # validated by AgentConfig
        has_incidents="incidents" in features,
        has_findings="findings" in features,
        has_coverage="coverage" in features,
        has_runs="runs" in features,
        has_pm="pm" in features,
    )


def build_registry(settings: Settings) -> list[AgentSpec]:
    """Return the ordered agents registry from the dashboard's declared config.

    Reads ``settings.agents`` (env-driven) — the dashboard runs with ANY
    composition of agents, including none.
    """
    return [_spec_from_config(cfg) for cfg in settings.agents]


def by_slug(registry: Iterable[AgentSpec], slug: str) -> AgentSpec | None:
    for spec in registry:
        if spec.slug == slug:
            return spec
    return None


def with_feature(registry: Iterable[AgentSpec], feature: str) -> AgentSpec | None:
    """First agent in the registry declaring ``feature`` (e.g. ``"incidents"``).

    Lets views resolve "the incidents agent" by capability rather than by a
    hardcoded slug, so the dashboard works for any agent set.
    """
    attr = f"has_{feature}"
    for spec in registry:
        if getattr(spec, attr, False):
            return spec
    return None
