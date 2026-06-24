### What & why

One paragraph. Link the US-NNN / BR-NNN / ADR ID this PR implements, or
write `no spec impact` and explain.

### Spec changes

List the `docs/*.md` files you edited, or `none`.

- `docs/...` — what changed
- `docs/...` — what changed

### Test plan

- Commands you ran (paste the actual command, not a description).
- Tests you added (file paths and the `@spec US-NNN` markers).
- Manual verification you did.

### Checklist

- [ ] `uv run ruff check .` green
- [ ] `uv run ruff format --check .` green
- [ ] `uv run mypy packages/agentkit/src agents/monitoring/src apps/dashboard/src` green
- [ ] `uv run pytest ...` green for the affected member(s)
- [ ] CHANGELOG entry added under `[Unreleased]`
- [ ] No new secrets / real tenant names / real customer data / `.env`
      files introduced
- [ ] If behavior changed: a US/BR/ADR was updated or added in the same PR
