"""Shared Messages decoding preserves malformed attempts for canonical rejection."""

from __future__ import annotations

from typing import Any

import pytest

from core.chat.streaming import StreamingAccumulator
from core.providers._messages_stream import AnthropicMessagesStreamDecoder


@pytest.mark.parametrize("name", [None, "", 42, {}])
def test_empty_tool_start_with_invalid_name_survives_for_rejection(name: object) -> None:
    decoder = AnthropicMessagesStreamDecoder()
    accumulator = StreamingAccumulator()
    events: list[dict[str, Any]] = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "invalid_call", "name": name, "input": {}},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "valid_call",
                "name": "status",
                "input": {},
            },
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    ]
    for event in events:
        for delta in decoder.normalize(event):
            accumulator.add_delta(delta)

    result = accumulator.finalize_assistant_fields()
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None
    assert [call["id"] for call in result.tool_calls] == ["invalid_call", "valid_call"]
    assert result.tool_calls[0]["rejection"]["code"] == "malformed_tool_call"
    assert "rejection" not in result.tool_calls[1]
