"""``platform agent add | list | remove`` — the registry write/read commands.

All three operate on the dashboard's view of the registry: ``add``/``remove``
stage a change in ``DASHBOARD_AGENTS`` (inert until ``platform up dashboard``),
``list`` renders exactly the ``AgentSpec``s the dashboard would build. Features
are validated against ``KNOWN_FEATURES`` and ``kind`` against ``AgentConfig``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dashboard.registry import KNOWN_FEATURES, AgentSpec, by_slug
from dashboard.settings import AgentConfig

from . import config

# Printed after every mutation: the registry is read once at dashboard start.
APPLY_NOTICE = "! registry changed — run `platform up dashboard` to apply"


def _normalize_features(features: list[str]) -> list[str]:
    """Lower/trim each feature (mirroring ``registry.py:53``) and reject unknowns.

    The dashboard would silently ignore an unknown feature; the CLI is the
    friendlier gate and fails fast so a typo never reaches config.
    """
    out: list[str] = []
    for raw in features:
        feat = raw.strip().lower()
        if feat not in KNOWN_FEATURES:
            raise ValueError(f"unknown feature {raw!r}; choose from {', '.join(KNOWN_FEATURES)}")
        out.append(feat)
    return out


def agent_add(
    *,
    slug: str,
    url: str,
    kind: str = "server",
    port: int = 0,
    title: str = "",
    features: list[str] | None = None,
    env_path: Path = config.DASHBOARD_ENV_PATH,
) -> int:
    """Append-or-replace ``slug`` in ``DASHBOARD_AGENTS`` and stage the change."""
    feats = _normalize_features(features or [])
    # AgentConfig validates ``kind`` (Literal) and the rest; raises before write.
    new = AgentConfig(slug=slug, url=url, kind=kind, port=port, title=title, features=feats)
    agents = config.load_agents(env_path)
    agents = config.upsert(agents, new)
    config.save_agents(env_path, agents)
    print(f"✓ wrote {config.DASHBOARD_AGENTS_KEY} in {env_path} ({len(agents)} agents)")
    print(APPLY_NOTICE)
    return 0


def agent_remove(*, slug: str, env_path: Path = config.DASHBOARD_ENV_PATH) -> int:
    """Drop ``slug`` from the registry; error (exit 1) if it is not present."""
    agents = config.load_agents(env_path)
    if by_slug(config.registry_for(agents), slug) is None:
        print(f"error: no agent with slug {slug!r}", file=sys.stderr)
        return 1
    agents = config.remove(agents, slug)
    config.save_agents(env_path, agents)
    print(f"✓ removed {slug!r} from {config.DASHBOARD_AGENTS_KEY} ({len(agents)} agents)")
    print(APPLY_NOTICE)
    return 0


def agent_list(*, as_json: bool = False, env_path: Path = config.DASHBOARD_ENV_PATH) -> int:
    """Print the registry as the dashboard will build it (table or ``--json``)."""
    agents = config.load_agents(env_path)
    registry = config.registry_for(agents)

    if as_json:
        print(
            json.dumps(
                [
                    {
                        "slug": s.slug,
                        "kind": s.kind,
                        "url": s.base_url,
                        "port": s.port,
                        "features": _features_of(s),
                    }
                    for s in registry
                ],
                separators=(",", ":"),
            )
        )
        return 0

    if not registry:
        print("(no agents registered)")
        return 0

    rows = [("slug", "kind", "url", "features", "port")]
    rows += [
        (s.slug, s.kind, s.base_url, ",".join(_features_of(s)) or "-", str(s.port))
        for s in registry
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return 0


def _features_of(spec: AgentSpec) -> list[str]:
    """Reverse-map an ``AgentSpec``'s ``has_*`` flags back to feature names."""
    return [f for f in KNOWN_FEATURES if getattr(spec, f"has_{f}", False)]
