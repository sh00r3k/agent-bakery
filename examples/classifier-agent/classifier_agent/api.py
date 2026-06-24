"""HTTP surface for classifier-agent on the shared FastAPI factory.

Inherits /healthz, /readyz, /metrics.json from create_app; adds POST /classify.
The LLMClient + compiled graph are built once in the lifespan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agentkit import LLMClient, create_app, get_logger
from fastapi import Body, FastAPI

from .graph import build_graph
from .settings import get_settings

log = get_logger("classifier-agent.api")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    llm = LLMClient.from_settings(settings)
    app.state.llm = llm
    app.state.graph = build_graph(
        llm,
        labels=settings.labels,
        fallback_label=settings.fallback_label,
        max_tokens=settings.classify_max_tokens,
        max_chars=settings.input_max_chars,
    )
    log.info("api.started", labels=settings.labels, fallback=settings.fallback_label)
    yield


# create_app is an untyped agentkit factory (resolves to Any under mypy --strict);
# pin the result to FastAPI so the route decorator below is seen as typed.
app: FastAPI = create_app(settings, title="classifier-agent", lifespan=lifespan)


@app.post("/classify")
async def classify(text: str = Body(..., embed=True)) -> dict[str, Any]:
    """Classify ``text`` into the closed label set (deterministic fallback)."""
    result = await app.state.graph.ainvoke({"text": text})
    log.info("api.classify", label=result.get("label"), fell_back=result.get("fell_back"))
    return {
        "label": result.get("label"),
        "confidence": result.get("confidence"),
        "rationale": result.get("rationale"),
        "fell_back": result.get("fell_back"),
    }
