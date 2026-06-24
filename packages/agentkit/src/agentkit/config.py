"""Shared settings layered on a self-hosted infra stack.

Each agent subclasses ``BaseAgentSettings`` and adds its own fields. The base
covers everything the shared layer provides: Postgres (pgvector), Redis,
RabbitMQ, the external chat-LLM gateway, local Ollama embeddings, JWT auth and
observability. Defaults match the existing `agent_backend` docker stack so an
agent runs against the real cluster with an almost-empty .env.

Env var naming: every field reads an UPPER_SNAKE env var of the same name; an
agent may also set ``AGENT_NAME`` which scopes the Postgres database, Redis DB
index and logger name.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- identity -----------------------------------------------------------
    agent_name: str = Field("agent", description="Short slug; scopes DB name, redis index, logs.")
    env: Literal["dev", "staging", "prod"] = "dev"

    # --- shared Postgres (pgvector/pgvector:pg16 on agent_backend) ----------
    # On the host this is agent-postgres-1; from another container use the
    # service hostname `postgres` on network `agent_backend`.
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "appuser"
    postgres_password: str = Field("", repr=False)
    # Per-agent database, created by infra/bootstrap.sql. Defaults to agent_name.
    postgres_db: str | None = None

    # --- shared Redis -------------------------------------------------------
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = Field("", repr=False)
    # Logical DB index; assign a distinct one per agent to avoid key collisions.
    redis_db: int = 0

    # --- shared RabbitMQ (alerts -> chat microservice, task fan-out) ----
    rabbitmq_url: str = Field(
        "", repr=False, description="Required. No insecure default — set RABBITMQ_URL explicitly."
    )

    # --- chat LLM: external OpenAI-compatible gateway (LiteLLM @ gateway.example.com)
    # Model names are the gateway's registry (claude-sonnet, gpt-5, deepseek-chat,
    # qwen3.5-plus, glm-5.1, ...) — NOT vendor-versioned ids. Check /v1/models.
    llm_base_url: str = "https://your-gateway.example.com/v1"
    llm_api_key: str = Field("", repr=False)
    # minimax-m3 is the service's recommended/default model — the standard.
    llm_model: str = "minimax-m3"
    llm_max_tokens: int = 2048
    # Per-request USD ceiling; LLMClient refuses/raises above it.
    llm_max_cost_usd: float = 0.10
    # Per-job cumulative USD cap (BR-007). When > 0, LLMClient tracks cumulative
    # spend and raises JobCostCapExceeded once the cap is reached. 0 = disabled.
    llm_job_cost_cap_usd: float = 0.0
    # Resilience: a slow/unreachable gateway must not hang an agent forever.
    # Total per-request timeout (seconds) and the OpenAI SDK's built-in retry
    # count for transient (connection/5xx/429) errors.
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # In-process circuit breaker around the LLM seam: after this many CONSECUTIVE
    # failures the breaker opens and calls fail fast for the cooldown window, then
    # half-open (one trial). 0 disables the breaker entirely.
    llm_breaker_threshold: int = 5
    llm_breaker_cooldown_s: float = 30.0
    # Cost meter fails CLOSED on unpriced models by default: an unknown model is
    # treated as a high sentinel price so the per-request USD ceiling still
    # triggers (a 1M-token call can't slip through unguarded). Set True to opt
    # back into metering unpriced models as $0 (the old, permissive behavior).
    allow_unpriced_models: bool = False

    # --- embeddings: local Ollama (nomic-embed-text), free ------------------
    embed_base_url: str = "http://host.docker.internal:11434/v1"
    embed_api_key: str = Field("ollama", repr=False)
    embed_model: str = "nomic-embed-text"

    # --- auth (host mints HS256 tokens; agents only verify) -----------------
    jwt_secret: str = Field("", repr=False)
    jwt_algorithms: list[str] = ["HS256"]
    jwt_audience: str | None = None

    # --- observability ------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    otel_exporter_otlp_endpoint: str = ""

    # --- Private Mode (v0.2) -------------------------------------------------
    # When True: zero outbound except the configured LLM gateway. otel
    # init is skipped, structlog stays file-only, and agentkit.egress.guard
    # any TCP/httpx call against the allow-list (LLM_BASE_URL, DATABASE_URL,
    # REDIS_URL, RABBITMQ_URL). Enforced in code, NOT at the firewall — combine
    # with firewall rules for true air-gap (docs/design-private-mode.md §8).
    private_mode: bool = False
    # Egress guard: hosts where mutating HTTP verbs (POST/PUT/DELETE/PATCH) are
    # explicitly permitted (BR-012). The LLM gateway host is auto-included.
    # Add other hosts here only if the agent must POST to them (e.g. a webhook).
    egress_safe_write_hosts: list[str] = []
    # When True (default): refuse to boot if private_mode is on AND env != "dev"
    # AND llm_base_url looks like a placeholder. Fail-closed (BR-011).
    egress_boot_gate: bool = True

    # --- outbound agent boot gate (BR-011) ----------------------------------
    # When private_mode is True and env is not "dev", the agent MUST have at
    # least one non-trivial egress allowlist entry (i.e. the LLM gateway must
    # be configured with a real host, not a placeholder). If this check fails
    # the agent refuses to start — fail-closed rather than running open.
    # Set to False to disable this gate (not recommended outside dev).

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_otel_enabled(self) -> bool:
        """OTel exporter is OFF when Private Mode is on."""
        return bool(self.otel_exporter_otlp_endpoint) and not self.private_mode

    # --- server -------------------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - container binds all interfaces; fronted by a proxy
    port: int = 8000
    # Defense-in-depth the shared factory wires into every agent (ARCH-009).
    # All closed by default; a same-origin HTMX dashboard / proxy-fronted agent
    # needs none of these, so opt-in only.
    # CORS: explicit browser-origin allowlist. Empty = no CORSMiddleware (the
    # safe same-origin default). NEVER set to ["*"] with credentials.
    cors_allow_origins: list[str] = []
    # TrustedHost: Host-header allowlist. Empty = middleware off (proxy enforces).
    trusted_hosts: list[str] = []
    # Per-IP token bucket on unauthenticated routes (e.g. webhooks). 0 = disabled.
    # Backed by Redis when reachable; falls back to an in-process bucket otherwise.
    # On by default (60/min) so an exposed agent is never accidentally unguarded;
    # set to 0 to disable explicitly.
    rate_limit_per_minute: int = 60
    # When fronted by a trusted reverse proxy, key the limiter on the leftmost
    # X-Forwarded-For hop (the real client) instead of the proxy's socket IP.
    # Leave False unless the proxy is trusted: a client can forge XFF otherwise,
    # letting an attacker dodge the limit by rotating the header.
    trust_proxy: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_name(self) -> str:
        return self.postgres_db or self.agent_name

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        # psycopg wants a libpq URL string.
        pw = f":{self.postgres_password}" if self.postgres_password else ""
        return (
            f"postgresql://{self.postgres_user}{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.database_name}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        pw = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{pw}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # Validation hooks so a typo'd DSN fails fast at startup, not first query.
    def validated_postgres_dsn(self) -> PostgresDsn:
        return PostgresDsn(self.database_url)

    def validated_redis_dsn(self) -> RedisDsn:
        return RedisDsn(self.redis_url)

    def validate_egress_boot_gate(self) -> None:
        """Fail-closed boot check (BR-011): when private_mode is on and env is
        not dev, the LLM gateway must point to a real host (not a placeholder).
        Raises RuntimeError if the gate fails. Call from create_app startup."""
        if not self.private_mode or not self.egress_boot_gate:
            return
        if self.env == "dev":
            return
        gateway_host = (
            (self.llm_base_url or "")
            .replace("https://", "")
            .replace("http://", "")
            .split("/")[0]
            .split(":")[0]
        )
        placeholder_patterns = ("example.com", "your-gateway", "localhost", "127.0.0.1", "0.0.0.0")  # noqa: S104
        if not gateway_host or any(p in gateway_host for p in placeholder_patterns):
            raise RuntimeError(
                f"Egress boot gate (BR-011): private_mode is on, env={self.env!r}, "
                f"but LLM_BASE_URL appears to be a placeholder ({self.llm_base_url!r}). "
                f"Set a real gateway URL or set EGRESS_BOOT_GATE=false (not recommended)."
            )


@lru_cache
def get_settings() -> BaseAgentSettings:
    """Cached base settings. Agents typically define their own cached getter
    returning their subclass instead of calling this directly."""
    # All fields have defaults / are env-sourced by pydantic-settings; mypy
    # can't see that constructor and flags the omitted args (call-arg).
    return BaseAgentSettings()  # type: ignore[call-arg]
