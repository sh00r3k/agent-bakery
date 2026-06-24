# Reference Dockerfile for the platform operator CLI. Same hardening as the
# long-running agents (multi-stage, digest-pinned, non-root, uv-locked) but
# CMD points at the CLI's --help — this is a published binary, not a service
# to healthcheck. Operators invoke a subcommand explicitly:
#   docker run --rm ghcr.io/<owner>/agent-bakery/platform-cli:0.2.0 doctor --json
#
# Build with the REPO ROOT as context so the workspace lockfile and the
# dashboard package (platform-cli depends on it) are available:
#   docker build -f apps/platform-cli/Dockerfile -t platform-cli .
#
# Base images are digest-pinned (not just tag-pinned) so rebuilds are
# reproducible and a moved tag can't silently swap the base out from under us.

# uv binary, pinned by digest (v0.11.21).
FROM ghcr.io/astral-sh/uv@sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946 AS uv

# python:3.12-slim, pinned by digest.
FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9 AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ---- builder: resolve the env from uv.lock (matches CI exactly) ----
FROM base AS builder
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# Workspace metadata + lockfile first, then sources, so dependency layers
# cache across source-only changes. platform-cli depends on dashboard, which
# depends on agentkit — COPY all three so the workspace resolves.
COPY pyproject.toml uv.lock ./
COPY packages/agentkit ./packages/agentkit
COPY apps/dashboard ./apps/dashboard
COPY apps/platform-cli ./apps/platform-cli

# --frozen: never re-resolve; --locked is asserted in CI. Install only this
# member (and its deps) from the lock into /app/.venv.
RUN uv sync --frozen --no-dev --package platform-cli

# ---- runtime: minimal, non-root, no uv/build tooling ----
FROM base AS runtime

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/packages/agentkit /app/packages/agentkit
COPY --from=builder /app/apps/dashboard /app/apps/dashboard
COPY --from=builder /app/apps/platform-cli /app/apps/platform-cli

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd -m -u 10001 agent
USER agent

# `platform` is the [project.scripts] console-script in
# apps/platform-cli/pyproject.toml. --help prints usage and exits 0 — a safe
# default that surfaces CLI changes on a bare `docker run` instead of doing
# nothing.
CMD ["platform", "--help"]
