"""The structured classification result the LLM must produce."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Classification(BaseModel):
    """One classification: a label, a confidence in ``[0, 1]``, and a rationale.

    ``label`` is validated against the agent's closed label set in the graph node
    (not here) so a schema-valid but off-set label is caught and replaced with the
    deterministic fallback.
    """

    label: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(default="")
