"""hello-agent — the smallest possible agentkit + LangGraph agent.

Shows the agents pattern in one file:
  1. subclass BaseAgentSettings for env-driven config
  2. build a 1-node LangGraph
  3. expose it over the shared FastAPI factory (create_app -> /healthz, /readyz)

Run from the example directory (the workspace root has no entry for this
loose example, so the cwd must be examples/hello-agent):

    cd examples/hello-agent
    uv run --project ../.. uvicorn hello_agent:app --reload

Then: curl -X POST localhost:8000/echo -H 'content-type: application/json' \\
           -d '{"text": "hi"}'
"""

from __future__ import annotations

from typing import Any, TypedDict

from agentkit import BaseAgentSettings, create_app, get_logger
from fastapi import Body
from langgraph.graph import END, START, StateGraph

log = get_logger("hello-agent")


class Settings(BaseAgentSettings):
    agent_name: str = "hello-agent"
    greeting: str = "hello"


class State(TypedDict):
    text: str
    reply: str


settings = Settings()


def respond(state: State) -> State:
    """The single graph node: turn input text into a greeting."""
    return {"text": state["text"], "reply": f"{settings.greeting}, {state['text']}!"}


def build_graph():
    g = StateGraph(State)
    g.add_node("respond", respond)
    g.add_edge(START, "respond")
    g.add_edge("respond", END)
    return g.compile()


_RECURSION_LIMIT = 10


def run_graph(graph: Any, state: dict) -> Any:
    """Invoke the compiled graph with a bounded recursion limit."""
    return graph.invoke(state, config={"recursion_limit": _RECURSION_LIMIT})


GRAPH = build_graph()
app = create_app(settings, title="hello-agent")


@app.post("/echo")
async def echo(text: str = Body(..., embed=True)) -> dict:
    result = GRAPH.invoke({"text": text, "reply": ""})
    log.info("echo", text=text)
    return result
