"""``platform`` — operator CLI for the agent platform (ADR-0011 / ADR-0009).

A thin, opinionated wrapper that:

- owns **only** the ``DASHBOARD_AGENTS`` key in ``apps/dashboard/.env`` — the
  single source of truth for the config-driven registry (US-013);
- drives the repo-root ``docker-compose.yml`` lifecycle (``up`` / ``down``);
- mints operator JWTs by shelling the existing ``mint-admin-token.py``;
- health-probes each registered agent's ``/healthz`` + ``/readyz``.

It is **not** a control plane: it never talks to a running dashboard, holds no
state, and re-uses the dashboard's :class:`~dashboard.settings.AgentConfig` /
registry rather than re-declaring them, so it can never become a second source
of truth.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
