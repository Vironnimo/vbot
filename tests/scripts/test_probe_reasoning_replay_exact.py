"""Regression tests for the exact Provider reasoning-replay probe."""

from __future__ import annotations

from scripts.probe_reasoning_replay_exact import _build_tool_calls


def test_build_tool_calls_accepts_canonical_vbot_shape() -> None:
    calls = _build_tool_calls(
        [{"id": "call_1", "name": "get_weather", "arguments": {"city": "Berlin"}}]
    )

    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "Berlin"}


def test_build_tool_calls_still_accepts_openai_wire_shape() -> None:
    calls = _build_tool_calls(
        [
            {
                "id": "call_2",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city":"Berlin"}',
                },
            }
        ]
    )

    assert len(calls) == 1
    assert calls[0].id == "call_2"
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "Berlin"}
