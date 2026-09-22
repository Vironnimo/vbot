"""Messages Usage preserves independent counters without inventing measurements."""

from __future__ import annotations

from typing import Any

import pytest

from core.providers._messages_stream import AnthropicMessagesStreamDecoder
from core.providers._messages_wire import _extract_anthropic_usage


@pytest.mark.parametrize("missing_field", ["input_tokens", "output_tokens"])
@pytest.mark.parametrize("invalid", [None, True, -1, "17"])
def test_completed_usage_preserves_only_usable_primary_counters(
    missing_field: str, invalid: Any
) -> None:
    usage = {"input_tokens": 17, "output_tokens": 8}
    expected = dict(usage)
    del expected[missing_field]
    usage[missing_field] = invalid
    assert _extract_anthropic_usage({"usage": usage}) == expected


@pytest.mark.parametrize("invalid", [True, False, -20, "17"])
def test_malformed_cache_counters_do_not_corrupt_input_measurement(invalid: Any) -> None:
    usage = {
        "input_tokens": 17,
        "output_tokens": 8,
        "cache_read_input_tokens": invalid,
        "cache_creation_input_tokens": invalid,
    }
    assert _extract_anthropic_usage({"usage": usage}) == {
        "input_tokens": 17,
        "output_tokens": 8,
    }
    decoder = AnthropicMessagesStreamDecoder()
    decoder.normalize({"type": "message_start", "message": {"usage": usage}})
    assert decoder.normalize({"type": "message_delta", "usage": {"output_tokens": 8}}) == [
        {"type": "usage", "input_tokens": 17, "output_tokens": 8}
    ]


@pytest.mark.parametrize("terminal_usage", [{}, {"output_tokens": None}, {"input_tokens": 23}])
def test_stream_preserves_measured_input_when_output_counter_is_absent(
    terminal_usage: dict[str, Any],
) -> None:
    decoder = AnthropicMessagesStreamDecoder()
    decoder.normalize({"type": "message_start", "message": {"usage": {"input_tokens": 17}}})
    assert decoder.normalize({"type": "message_delta", "usage": terminal_usage}) == [
        {"type": "usage", "input_tokens": terminal_usage.get("input_tokens", 17)}
    ]
