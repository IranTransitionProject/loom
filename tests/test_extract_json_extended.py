"""Extended tests for _extract_json() — covers the full fallback chain.

Complements test_extract_json.py (which focuses on YAML paths) with
additional coverage for JSON parsing, fence stripping, brace extraction,
nested objects, and error cases.
"""

import pytest

from heddle.worker.runner import _extract_json

# --- Step 1: Direct valid JSON (no fences, no extras) ---


class TestDirectJSON:
    def test_simple_object(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_with_whitespace_padding(self):
        raw = '   \n  {"status": "ok"}  \n  '
        assert _extract_json(raw) == {"status": "ok"}

    def test_complex_types(self):
        raw = '{"items": [1, 2, 3], "nested": {"x": true}, "n": null}'
        result = _extract_json(raw)
        assert result["items"] == [1, 2, 3]
        assert result["nested"]["x"] is True
        assert result["n"] is None

    def test_unicode_values(self):
        raw = '{"greeting": "\\u0048ello"}'
        assert _extract_json(raw)["greeting"] == "Hello"


# --- Step 2: JSON in ```json fences ---


class TestJSONFenced:
    def test_json_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        assert _extract_json(raw) == {"key": "value"}

    def test_json_fence_multiline(self):
        raw = '```json\n{\n  "a": 1,\n  "b": 2\n}\n```'
        result = _extract_json(raw)
        assert result == {"a": 1, "b": 2}

    def test_json_fence_with_preamble(self):
        raw = 'Here is the result:\n\n```json\n{"ok": true}\n```'
        assert _extract_json(raw) == {"ok": True}

    def test_json_fence_with_trailing_text(self):
        raw = '```json\n{"ok": true}\n```\n\nHope this helps!'
        assert _extract_json(raw) == {"ok": True}


# --- Step 3: JSON in bare ``` fences (no language tag) ---


class TestBareFence:
    def test_bare_fence(self):
        raw = '```\n{"bare": true}\n```'
        assert _extract_json(raw) == {"bare": True}

    def test_bare_fence_with_preamble(self):
        raw = 'I generated this:\n```\n{"result": 42}\n```'
        assert _extract_json(raw) == {"result": 42}


# --- Step 4: JSON with extra text before/after braces ---


class TestBraceExtraction:
    def test_text_before_json(self):
        raw = 'Sure, here is the output: {"answer": 42}'
        assert _extract_json(raw) == {"answer": 42}

    def test_text_after_json(self):
        raw = '{"answer": 42} Let me know if you need anything else.'
        assert _extract_json(raw) == {"answer": 42}

    def test_text_surrounding_json(self):
        raw = 'Here you go:\n{"x": 1}\nDone!'
        assert _extract_json(raw) == {"x": 1}

    def test_multiline_json_in_prose(self):
        raw = (
            "I have analyzed the text. Here is my response:\n"
            '{"summary": "A short summary", "confidence": 0.95}\n'
            "Let me know if you need more detail."
        )
        result = _extract_json(raw)
        assert result["summary"] == "A short summary"
        assert result["confidence"] == 0.95


# --- Step 5: YAML in ```yaml fences (parsed via YAML fallback) ---


class TestYAMLFenced:
    def test_yaml_fence_nested(self):
        raw = "```yaml\nresult:\n  items:\n    - name: a\n    - name: b\n```"
        result = _extract_json(raw)
        assert result["result"]["items"] == [{"name": "a"}, {"name": "b"}]

    def test_yaml_fence_mixed_types(self):
        raw = "```yaml\ncount: 5\nactive: true\nlabel: hello\n```"
        result = _extract_json(raw)
        assert result == {"count": 5, "active": True, "label": "hello"}


# --- Step 6: Plain YAML (no fences) ---


class TestPlainYAML:
    def test_plain_yaml_multikey(self):
        raw = "status: success\ncount: 10\ntags:\n  - alpha\n  - beta"
        result = _extract_json(raw)
        assert result["status"] == "success"
        assert result["count"] == 10
        assert result["tags"] == ["alpha", "beta"]


# --- Step 7: Completely unparseable content ---


class TestUnparseable:
    def test_plain_text_raises(self):
        with pytest.raises(ValueError, match="non-JSON"):
            _extract_json("This is just plain English text with no structure.")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _extract_json("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            _extract_json("   \n\n   ")

    def test_yaml_list_at_top_level_raises(self):
        """YAML that parses to a list (not dict) should raise."""
        raw = "- one\n- two\n- three"
        with pytest.raises(ValueError):
            _extract_json(raw)

    def test_yaml_scalar_raises(self):
        """YAML that parses to a scalar string should raise."""
        raw = "```yaml\njust a string\n```"
        with pytest.raises(ValueError):
            _extract_json(raw)


# --- Step 8: Nested JSON objects ---


class TestNestedJSON:
    def test_deeply_nested(self):
        raw = '{"a": {"b": {"c": {"d": 1}}}}'
        assert _extract_json(raw)["a"]["b"]["c"]["d"] == 1

    def test_nested_in_fence(self):
        raw = '```json\n{"outer": {"inner": [1, 2, 3]}}\n```'
        result = _extract_json(raw)
        assert result["outer"]["inner"] == [1, 2, 3]

    def test_nested_extracted_from_prose(self):
        raw = 'Result: {"data": {"records": [{"id": 1}, {"id": 2}]}}'
        result = _extract_json(raw)
        assert len(result["data"]["records"]) == 2
        assert result["data"]["records"][0]["id"] == 1


# --- Step 9: Common LLM mistakes ---


class TestLLMQuirks:
    def test_trailing_comma_in_object(self):
        """Trailing commas are invalid JSON; the brace-extraction regex
        will capture the block but json.loads will reject it. If yaml is
        installed it may still parse via the YAML fallback path (YAML
        tolerates trailing commas in flow mappings depending on the
        parser). If not, ValueError is expected."""
        raw = '{"a": 1, "b": 2,}'
        # json.loads rejects this. The { ... } extraction will find it
        # but also fail. YAML safe_load on the raw string may or may not
        # parse it depending on the PyYAML version. We just verify we
        # get either a valid dict or a ValueError — no other exception.
        try:
            result = _extract_json(raw)
            assert isinstance(result, dict)
        except ValueError:
            pass  # acceptable outcome

    def test_single_quoted_strings_via_yaml(self):
        """Single-quoted strings are invalid JSON but valid YAML."""
        raw = "key: 'value'"
        result = _extract_json(raw)
        assert result == {"key": "value"}

    def test_unquoted_keys_via_yaml(self):
        """Unquoted keys are invalid JSON but valid YAML."""
        raw = "status: ok\ncount: 3"
        result = _extract_json(raw)
        assert result["status"] == "ok"

    def test_fence_with_extra_spaces(self):
        """Some LLMs add spaces between ``` and language tag."""
        raw = '```json  \n{"ok": true}\n```'
        # The regex expects ```json (no space before newline in tag),
        # but the brace extraction fallback should still work.
        result = _extract_json(raw)
        assert result == {"ok": True}

    def test_multiple_json_blocks_returns_first_balanced(self):
        """B3 fix: with two JSON objects in prose, the brace-balanced scan
        returns the FIRST balanced object.  The previous greedy ``\\{.*\\}``
        regex spanned both objects into an ill-formed superstring and bounced
        through to the YAML fallback (also failing), so the worker raised
        ``ValueError`` on a perfectly recoverable response.  Returning the
        first object matches "the LLM produced its answer, then narrated"
        — the common case."""
        raw = 'First: {"a": 1} and second: {"b": 2}'
        assert _extract_json(raw) == {"a": 1}

    def test_single_json_block_in_prose_succeeds(self):
        """A single JSON object surrounded by prose is extracted fine."""
        raw = 'The result is {"a": 1}. That is all.'
        assert _extract_json(raw) == {"a": 1}

    def test_prose_with_stray_brace_before_json(self):
        """B3 adjacent contract: stray ``}`` in prose before the real JSON
        object must NOT confuse the balanced scan into capturing garbage.
        The pre-fix greedy regex did not have this failure mode (it always
        anchored at first ``{``), but a naive depth scanner that decrements
        below zero would.  Pin the guard."""
        raw = 'Note: closing brace } is fine. Result: {"answer": 42}'
        assert _extract_json(raw) == {"answer": 42}

    def test_brace_inside_string_literal_does_not_unbalance(self):
        """B3 adjacent contract: braces inside JSON string values must not
        affect depth tracking.  A simpler scanner that ignores strings
        would call ``{"text": "}"}`` unbalanced after the first ``}``."""
        raw = 'Output: {"text": "has } and { inside"} and trailing prose.'
        assert _extract_json(raw) == {"text": "has } and { inside"}

    def test_nested_object_with_following_object_returns_outer_first(self):
        """B3: nested ``{}`` inside the first object don't terminate it,
        and a later second object is ignored — the FIRST balanced outer
        block wins."""
        raw = '{"outer": {"inner": 1}} then {"second": 2}'
        result = _extract_json(raw)
        assert result == {"outer": {"inner": 1}}

    def test_escaped_quote_in_string_does_not_close_string_state(self):
        """B3 adjacent contract: ``\\"`` inside a string literal doesn't
        end the string, so braces after it are still treated as inside-string."""
        raw = '{"q": "she said \\"hi {nested}\\" loud"}'
        result = _extract_json(raw)
        assert result["q"] == 'she said "hi {nested}" loud'


# --- Regex edge cases ---


class TestFenceRegex:
    def test_fence_with_no_newline_before_close(self):
        raw = '```json\n{"x": 1}```'
        result = _extract_json(raw)
        assert result == {"x": 1}

    def test_nested_backticks_in_value(self):
        """JSON value containing backticks should not confuse fence regex
        when the JSON is not fenced."""
        raw = '{"code": "use `fmt.Println`"}'
        result = _extract_json(raw)
        assert result["code"] == "use `fmt.Println`"
