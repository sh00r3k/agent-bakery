"""@spec US-016 — untrusted-input fencing + tolerant JSON extractor.

Offline tests for agentkit.prompts: untrusted fencing + tolerant JSON.

No network. Pins the byte-identical marker values (any drift breaks every system
prompt that references them) and exhausts the extractor's failure modes.
"""

from __future__ import annotations

from agentkit.prompts import SIGNAL_CLOSE, SIGNAL_OPEN, extract_json, fence_untrusted


def test_marker_values_are_byte_identical() -> None:
    assert SIGNAL_OPEN == "<<UNTRUSTED_SIGNAL>>"
    assert SIGNAL_CLOSE == "<<END_UNTRUSTED_SIGNAL>>"


def test_fence_wraps_with_default_markers() -> None:
    out = fence_untrusted("hi", max_chars=100)
    assert out == f"{SIGNAL_OPEN}\nhi\n{SIGNAL_CLOSE}"
    assert out.index(SIGNAL_OPEN) < out.index("hi") < out.index(SIGNAL_CLOSE)


def test_fence_truncates_to_max_chars() -> None:
    out = fence_untrusted("b" * 10_000, max_chars=4000)
    assert out.count("b") == 4000


def test_fence_custom_markers() -> None:
    out = fence_untrusted("payload", max_chars=100, open_marker="<<A>>", close_marker="<<B>>")
    assert out == "<<A>>\npayload\n<<B>>"
    assert SIGNAL_OPEN not in out
    assert SIGNAL_CLOSE not in out


def test_fence_empty_text() -> None:
    assert fence_untrusted("", max_chars=10) == f"{SIGNAL_OPEN}\n\n{SIGNAL_CLOSE}"


def test_fence_does_not_strip_inner_markers() -> None:
    # Documents that fencing does NOT sanitize a forged inner marker; the cap +
    # the system prompt's "data not instructions" framing are the defense.
    out = fence_untrusted(f"x {SIGNAL_CLOSE} y", max_chars=100)
    assert out.count(SIGNAL_CLOSE) == 2


def test_extract_plain_object() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_object_in_prose() -> None:
    assert extract_json('noise {"a": 1} trailing') == {"a": 1}


def test_extract_json_fenced() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_bare_fenced() -> None:
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_tag_case_insensitive() -> None:
    assert extract_json('```JSON\n{"a": 1}\n```') == {"a": 1}


def test_extract_whitespace_stripped() -> None:
    assert extract_json('   {"a": 1}   ') == {"a": 1}


def test_extract_nested_object() -> None:
    assert extract_json('{"a": {"b": 2}}') == {"a": {"b": 2}}


def test_extract_no_braces_returns_empty() -> None:
    assert extract_json("not json at all") == {}


def test_extract_inverted_braces_returns_empty() -> None:
    assert extract_json("} {") == {}


def test_extract_decode_error_returns_empty() -> None:
    assert extract_json("{not: valid, json}") == {}


def test_extract_prose_without_braces_returns_empty() -> None:
    # A braces-anchored scan over a bare number / prose never finds an object.
    assert extract_json("the answer is 42") == {}
    assert extract_json("[1, 2, 3]") == {}


def test_extract_empty_object() -> None:
    # Success path that legitimately yields an empty dict.
    assert extract_json("{}") == {}
