# Reference Dockerfile for the ops dashboard. Slim, non-root, lockfile-faithful.
# Build with the REPO ROOT as context so the workspace lockfile and the
# agentkit package are available:
#   docker build -f apps/dashboard/Dockerfile -t dashboard .
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
# cache across source-only changes.
COPY pyproject.toml uv.lock ./
COPY packages/agentkit ./packages/agentkit
COPY apps/dashboard ./apps/dashboard

# --frozen: never re-resolve; --locked is asserted in CI. Install only this
# member (and its deps) from the lock into /app/.venv.
RUN uv sync --frozen --no-dev --package dashboard

# ---- runtime: minimal, non-root, no uv/build tooling ----
FROM base AS runtime

# psycopg[binary] ships its own libpq; only curl is needed for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/packages/agentkit /app/packages/agentkit
COPY --from=builder /app/apps/dashboard /app/apps/dashboard

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd -m -u 10001 agent
USER agent

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["python", "-m", "dashboard"]
