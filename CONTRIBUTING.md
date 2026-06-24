# Contributing to agent-bakery

Thanks for your interest. This project follows **Spec-Driven Development**:
behavior changes start in `docs/`, code follows. Read
[`docs/README.md`](docs/README.md) before opening a PR — it is the entry
point to the whole spec chain.

## What we welcome

- Bug reports with a real repro (see the issue template).
- PRs that fix a bug, implement a documented US/BR, or correct a doc drift.
- ADRs that argue for a different decision than the existing one in
  [`docs/adr/`](docs/adr/) (append-only; a new ADR supersedes the old).
- Spec-first proposals: a PR that adds or extends a US/BR/ADR in `docs/`
  before the code lands.

## What we do not accept

- Drive-by typo PRs with no spec impact.
- Refactors that are not justified by a specific US/BR/ADR/issue.
- PRs that introduce a new top-level dependency without an ADR.
- "Marketing" changes to docs that are not grounded in the code or spec.

## Filing issues

- **Bugs** — use the bug-report template. Provide a minimal repro, the
  exact version/commit, the gateway URL (placeholder is fine), and the
  relevant structlog lines. **Redact secrets, real tenant names, and
  real URLs before pasting.**
- **Feature requests** — use the feature-request template. The strongest
  proposals cite a US/BR ID or propose a new one. Spec impact is not
  optional; a feature without a spec item is a feature without a contract.
- **Questions** — use GitHub Discussions, not the issue tracker. The
  issue tracker is for actionable work only.

## Submitting a PR

1. Branch from `main`. One logical change per PR. Squash-merge is fine.
2. **Spec first if behavior changes.** A US-NNN, a BR-NNN, a new entity
   in `docs/domain-model.md`, or a new ADR must land in the same PR as
   the code (or in a clearly linked earlier PR). A code change without
   a spec change is a bug in the spec.
3. The PR body must include:
   - **What & why** — one paragraph, link the US/BR/ADR ID.
   - **Spec changes** — list of `docs/*.md` files edited, or `none`.
   - **Test plan** — commands you ran, tests you added, `@spec US-NNN`
     markers on the new tests.
4. CI must be green before review. If CI is red, the PR is "WIP" and
   will not be reviewed.
5. Expect a first response within ~3 business days. No response within
   7 days is a signal to ping in the PR or in Discussions.

## Development setup

```bash
# install the whole workspace editable
uv sync

# run tests for a single member
uv run pytest packages/agentkit/tests
uv run pytest agents/monitoring/tests
uv run pytest apps/dashboard/tests

# lint + format
uv run ruff check .
uv run ruff format --check .
uv run mypy packages/agentkit/src agents/monitoring/src apps/dashboard/src
```

The full workspace installs in one environment because the root
`pyproject.toml` declares `[tool.uv.workspace]` with all members.

## Project layout

```
agent-bakery/
├── packages/agentkit      # the shared toolkit (keystone — every member imports it)
├── agents/monitoring      # agent meta-monitor (scheduled probes → alerts)
├── apps/dashboard         # HTMX ops console (HTTP fan-out across the agents)
├── examples/hello-agent   # ~40-line reference agent
├── infra/                 # bootstrap.sql, Caddy edge, socket-proxy
├── docs/                  # the SDD spec — read docs/README.md first
├── pyproject.toml         # uv workspace root
└── AGENTS.md              # project rules (spec ahead of code, no drive-by refactors, etc.)
```

## Coding conventions

The lint and type-check posture is the convention. Read
`pyproject.toml` for the exact rules.

- **Python 3.12+**, type hints on all public functions. `mypy --strict`
  is the bar; tests are exempt (see `pyproject.toml` `[tool.mypy.overrides]`).
- **Ruff** is the linter and formatter. The rule set is in
  `pyproject.toml` (`E, F, I, B, UP, S, ASYNC, SIM, RUF`). `B008` is
  disabled because FastAPI's `Depends(...)`-in-default idiom is
  legitimate.
- **No `print` in production code.** Use `structlog` via
  `agentkit.observability`.
- **No swallowed exceptions.** `except: pass` and bare
  `except Exception` without re-raise or explicit context is a CI
  failure.
- **Pydantic models at the boundaries.** Tool inputs, HTTP payloads,
  env-derived settings — all typed Pydantic models. Internal functions
  may use primitive types.
- **Per-tenant isolation by construction.** Every store call is scoped
  by `tenant_id` from the verified `Principal` (BR-002). The only
  cross-tenant principal is `ops` (US-013).

## Release process

- Semver. Tags are the source of truth (no release branches in v0.x).
- CHANGELOG entry is **required** under `[Unreleased]` in the PR.
  Maintainer moves it to a dated version on release.
- `uv build && uv publish` from the member directory
  (`packages/agentkit`, `agents/monitoring`, `apps/dashboard`).
  The root is a workspace aggregator and is not published.

## Governance

BDFL model: decisions land in
[`docs/adr/`](docs/adr/) (append-only) and are referenced from the spec
docs. A new ADR that supersedes an old one links back to it. The
maintainer is the only committer with merge rights in v0.x; we add
maintainers as the contributor base grows.

## Code of conduct

By participating, you agree to the [Contributor Covenant](CODE_OF_CONDUCT.md).
Reports go to the maintainer handle listed in the README.
