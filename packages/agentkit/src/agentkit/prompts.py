"""Shared LLM prompt helpers: untrusted-text fencing + tolerant JSON parsing.

Two agent-agnostic primitives factored out of the monitoring agent so every
agent shares one prompt-safety contract:

- :func:`fence_untrusted` wraps attacker-controlled text in OPEN/CLOSE markers
  and truncates it to a cap (AF-01 indirect prompt injection + AF-08 unbounded
  consumption). The caller's system prompt declares everything between the
  markers is UNTRUSTED DATA, never instructions.
- :func:`extract_json` pulls a JSON object out of a possibly fenced/prose-wrapped
  LLM response, returning ``{}`` on any failure so callers can apply a
  deterministic fallback.

The marker constants are **byte-identical** to the monitoring agent's historical
delimiters: any system prompt that references them by literal string (and the
tests asserting they appear in the fenced user content) keep matching.
"""

from __future__ import annotations

import json
from typing import Any

# Byte-identical to the monitoring agent's historical markers — changing these
# breaks any system prompt that references them by literal string (and the
# monitoring graph test that asserts them in the fenced user content).
SIGNAL_OPEN = "<<UNTRUSTED_SIGNAL>>"
SIGNAL_CLOSE = "<<END_UNTRUSTED_SIGNAL>>"


def fence_untrusted(
    text: str,
    *,
    max_chars: int,
    open_marker: str = SIGNAL_OPEN,
    close_marker: str = SIGNAL_CLOSE,
) -> str:
    """Truncate ``text`` to ``max_chars`` and wrap it between fence markers.

    The returned block is ``{open_marker}\\n{text[:max_chars]}\\n{close_marker}``.
    Use for any single untrusted blob fed to a model; the caller's system prompt
    must declare the fenced region is data to classify, never instructions to
    obey (AF-01). The hard cap bounds input tokens (AF-08).
    """
    return f"{open_marker}\n{text[:max_chars]}\n{close_marker}"


def extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from an LLM response, tolerating code fences.

    Strips a leading ```` ```json ```` / ```` ``` ```` fence (the ``json`` tag is
    matched case-insensitively), then scans from the first ``{`` to the last
    ``}`` so surrounding prose is tolerated. Returns ``{}`` on EVERY failure: no
    braces (so a bare list or number yields ``{}``), inverted braces, or a
    :class:`json.JSONDecodeError`. The final ``isinstance`` check is a defensive
    backstop — because the scan is anchored on braces, a successful parse is
    already an object.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}
