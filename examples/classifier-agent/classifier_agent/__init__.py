"""classifier-agent — reference agentkit + LangGraph text classifier.

Demonstrates the safe-LLM-I/O pattern end to end using the shared toolkit:

1. :func:`agentkit.fence_untrusted` wraps the caller's text in UNTRUSTED markers
   and caps its length (AF-01 prompt injection + AF-08 token blow-up).
2. :meth:`agentkit.LLMClient.complete_json` returns a schema-validated
   :class:`classifier_agent.schema.Classification` (with bounded corrective
   retry) instead of a raw string the caller must parse.
3. The graph node falls back deterministically to a fixed label on ANY model /
   validation failure or an off-set label — never propagates an LLM error.

Unlike the monitoring agent (which hand-rolled JSON parsing + fencing), this one
consumes the agentkit primitives directly: it is the canonical example of how an
agent should do prompt-safe, schema-validated LLM I/O.
"""

from __future__ import annotations

__version__ = "0.1.0"
