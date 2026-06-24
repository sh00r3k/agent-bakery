# Add your own agent

Every agent is a workspace member that imports `agentkit`. The pattern is the
same for all of them — see `examples/hello-agent/` for a ~40-line working agent.

> This is the quickstart. For the full contract — the platform endpoints, the
> `features`→panel mapping, registration, the SUT-safety capability, and how to
> extract an agent into its own repo — see [`agent-standard.md`](agent-standard.md).

## 1. Scaffold the package

```
agents/my-agent/
├── pyproject.toml
├── env.example
├── README.md
└── src/my_agent/
    ├── __init__.py
    ├── settings.py     # subclass BaseAgentSettings
    ├── graph.py        # your LangGraph
    └── api.py          # create_app() + routes  (or __main__.py for a CLI/worker)
```

The workspace root already globs `agents/*`, so a new directory is picked up
automatically.

## 2. `pyproject.toml`

```toml
[project]
name = "my-agent"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["agentkit", "langgraph>=0.2"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/my_agent"]

[tool.uv.sources]
agentkit = { workspace = true }
```

## 3. Settings (env-driven)

```python
from agentkit import BaseAgentSettings

class Settings(BaseAgentSettings):
    agent_name: str = "my-agent"
    # add your own fields; each reads an UPPER_SNAKE env var of the same name
```

## 4. Graph + app

```python
from langgraph.graph import START, END, StateGraph
from agentkit import create_app

GRAPH = build_graph()              # a compiled StateGraph
app = create_app(Settings(), title="my-agent")   # /healthz /readyz /metrics.json
```

For a **scheduled** agent (no HTTP surface), expose a `main()` in `__main__.py`
and drive the graph from APScheduler instead of FastAPI routes — see
`agents/monitoring`.

## 5. Wire it up

- Add the agent's DB to `infra/bootstrap.sql`.
- Add an `env.example`.
- Register it on the dashboard with the `platform` CLI (`apps/platform-cli`), which
  owns the `DASHBOARD_AGENTS` registry key:

  ```bash
  platform agent add <slug> --url https://my-agent.internal:8000 [--feature <name> ...]
  ```
- Add it to the CI matrix in `.github/workflows/ci.yml`.

## 6. Test

```bash
uv run pytest agents/my-agent/tests -q
```
