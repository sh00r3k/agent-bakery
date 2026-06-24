"""Dashboard settings extending the shared agentkit base.

The dashboard is a *peer agent* (built FROM agentkit) whose "graph" is HTTP
fan-out, not an LLM graph. It owns its own DB ``dashboard`` (redis idx 4) and is
an HTTP **client** of every other agent — it never reads their Postgres DBs.

The agent set is **fully config-driven**: :attr:`Settings.agents` is a
list of :class:`AgentConfig` entries (slug / url / kind / features). Nothing
about the agents is hardcoded — register, remove, or reorder agents purely by
setting the ``DASHBOARD_AGENTS`` env var (a JSON array) or overriding the
default below. On the host the dashboard reaches siblings over the shared docker
network by service hostname (``http://monitoring:8000`` etc.); locally / in tests
these come from the same config.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from agentkit import BaseAgentSettings
from pydantic import AliasChoices, BaseModel, Field, computed_field

AgentKind = Literal["server", "batch", "self"]


class AgentConfig(BaseModel):
    """One agents member the dashboard observes.

    ``kind``:
      - ``server`` — always-on HTTP agent (has a port, /healthz etc.).
      - ``batch``  — no port; freshness comes from a last_run heartbeat.
      - ``self``   — the dashboard itself (rendered as a tile, excluded from cost).
    ``features`` — capabilities the agent exposes; drives which panels/links the
    dashboard renders for it. Recognized: ``incidents``, ``findings``,
    ``coverage`` (a QA tester — its own findings feed + a /coverage rollup),
    ``runs``, ``pm`` (unknown values are ignored).
    """

    slug: str
    url: str
    kind: AgentKind = "server"
    title: str = ""
    port: int = 0
    features: list[str] = Field(default_factory=list)


# Default agent set (a representative example). Override entirely via the
# ``DASHBOARD_AGENTS`` env var (JSON array of objects with these same keys). The
# dashboard imposes NO requirement that any particular agent be present.
DEFAULT_AGENTS: list[AgentConfig] = [
    AgentConfig(
        slug="monitoring",
        title="monitoring",
        url="http://monitoring:8000",
        port=8002,
        kind="server",
        features=["incidents"],
    ),
    AgentConfig(
        slug="security",
        title="security",
        url="http://security:8000",
        port=8003,
        kind="server",
        features=["findings"],
    ),
    AgentConfig(
        slug="pm",
        title="pm",
        url="http://pm:8000",
        port=8001,
        kind="server",
        features=["pm"],
    ),
    AgentConfig(
        slug="web-ext-pipeline",
        title="web-ext-pipeline",
        url="http://web-ext-pipeline:8000",
        port=0,
        kind="batch",
        features=["runs"],
    ),
    AgentConfig(
        slug="ultraqa",
        title="ultraqa",
        url="http://ultraqa:8000",
        port=8007,
        kind="server",
        # QA tester: its own findings feed + a /coverage rollup. ``coverage``
        # flags it as the QA panel's provider so the dashboard resolves it by
        # capability, not by the literal "ultraqa" slug.
        features=["findings", "coverage"],
    ),
    AgentConfig(
        slug="dashboard",
        title="dashboard (self)",
        url="http://dashboard:8000",
        port=8005,
        kind="self",
    ),
]


class Settings(BaseAgentSettings):  # type: ignore[misc]  # untyped agentkit base
    agent_name: str = "dashboard"
    # Own DB + redis index.
    postgres_db: str | None = "dashboard"
    redis_db: int = 4
    port: int = 8000  # inside the container; host maps a loopback port -> 8000

    # --- agents registry (fully config/env-driven) --------------------------
    # A JSON array in DASHBOARD_AGENTS overrides this default entirely, e.g.:
    #   DASHBOARD_AGENTS='[{"slug":"monitoring","url":"http://monitoring:8000",
    #                       "kind":"server","features":["incidents"]}]'
    agents: list[AgentConfig] = Field(
        default_factory=lambda: list(DEFAULT_AGENTS),
        # Accept either DASHBOARD_AGENTS (preferred, namespaced) or AGENTS.
        validation_alias=AliasChoices("dashboard_agents", "agents"),
    )

    # --- branding (config/env-driven) --------------------------------------
    # The masthead / login / page-title brand. Generic by default; a deployment
    # sets DASHBOARD_BRAND to its own name via env.
    brand: str = Field(
        "agents",
        validation_alias=AliasChoices("dashboard_brand", "brand"),
    )

    # --- upstream auth ------------------------------------------------------
    # The dashboard mints its OWN service admin token (role=admin) signed with the
    # shared JWT secret to call agents' admin endpoints. Lifetime in seconds.
    upstream_token_ttl_s: int = 3600
    upstream_token_sub: str = "dashboard"  # noqa: S105 - JWT subject claim, not a secret

    # --- browser session ----------------------------------------------------
    # HttpOnly session cookie carrying the verified admin JWT.
    session_cookie_name: str = "agents_dash"
    # Readable (non-HttpOnly) companion cookie carrying the signed CSRF token for
    # the double-submit check (Plan 4 §4 hardening). JS/HTMX must read it, so it
    # is intentionally NOT HttpOnly; forgery is prevented by the HMAC signature.
    csrf_cookie_name: str = "agents_csrf"
    # Secure flag for both cookies. The session cookie carries the raw admin JWT,
    # so it MUST be Secure outside dev. ``None`` (the default) means "derive from
    # env": Secure everywhere except ``env=="dev"`` (plain-http local). Set an
    # explicit bool via DASHBOARD_SESSION_COOKIE_SECURE to override the auto rule.
    session_cookie_secure: bool | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_session_cookie_secure(self) -> bool:
        """Resolve the cookie ``Secure`` flag. Explicit bool wins; otherwise
        Secure unless ``env=="dev"`` (the only place plain http is expected)."""
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return bool(self.env != "dev")

    # --- aggregation layer --------------------------------------------------
    upstream_timeout_s: float = 3.0  # per-agent httpx timeout
    cache_ttl_s: int = Field(8, description="Redis TTL for panel responses (s).")


@lru_cache
def get_settings() -> Settings:
    return Settings()
