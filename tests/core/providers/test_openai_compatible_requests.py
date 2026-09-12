"""Openai compatible requests: messages behavior."""

from __future__ import annotations

from core.providers._http_shared import PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS

from .openai_compatible_test_support import (
    IMAGE_WIRE_MEDIA_TYPES,
    _to_openai_assistant_message,
)
from .openai_compatible_test_support import openai_adapter as openai_adapter
from .openai_compatible_test_support import openrouter_adapter as openrouter_adapter


def test_client_timeout_bounds_non_streaming_generation_reads(openai_adapter):
    timeout = openai_adapter._client.timeout  # noqa: SLF001 - verify adapter wiring.

    assert timeout.connect == 60.0
    assert timeout.read == PROVIDER_NON_STREAMING_READ_TIMEOUT_SECONDS
    assert timeout.write == 60.0
    assert timeout.pool == 60.0


def test_reasoning_replay_policy_defaults_to_full_history(openai_adapter):
    assert openai_adapter.reasoning_replay_policy("gpt-4o") == "full_history"


def test_wire_media_support_is_images_plus_openai_audio(openai_adapter):
    """The generic OpenAI-compatible wire carries images plus WAV/MP3 — no PDF.

    Generic providers (OpenRouter, MiniMax, OpenCode-Go, Mistral) inherit this set.
    """
    supported = openai_adapter.wire_media_support("gpt-4o")

    assert supported == IMAGE_WIRE_MEDIA_TYPES | frozenset({"audio/wav", "audio/mpeg"})
    assert "application/pdf" not in supported


class TestAssistantMessageFormatting:
    """Verify assistant wire-message formatting edge cases."""

    def test_assistant_message_without_tool_calls_uses_empty_content_string(self) -> None:
        wire_message = _to_openai_assistant_message(
            {
                "role": "assistant",
                "content": None,
            }
        )

        assert wire_message["content"] == ""
        assert "tool_calls" not in wire_message

    def test_assistant_message_with_tool_calls_keeps_null_content(self) -> None:
        wire_message = _to_openai_assistant_message(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "name": "read",
                        "arguments": {"path": "README.md"},
                    }
                ],
            }
        )

        assert wire_message["content"] is None
        assert wire_message["tool_calls"] == [
            {
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"path":"README.md"}',
                },
            }
        ]

    def test_recovered_argument_sequence_replays_as_correlated_sibling_calls(self) -> None:
        wire_message = _to_openai_assistant_message(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_batch",
                        "name": "bash",
                        "arguments": {"command": "echo one"},
                        "argument_sequence_index": 0,
                        "argument_sequence_length": 2,
                    },
                    {
                        "id": "tool_call_recovered_1234",
                        "name": "bash",
                        "arguments": {"command": "echo two"},
                        "argument_sequence_index": 1,
                        "argument_sequence_length": 2,
                    },
                ],
            }
        )

        assert [call["id"] for call in wire_message["tool_calls"]] == [
            "call_batch",
            "tool_call_recovered_1234",
        ]
        assert [call["function"]["arguments"] for call in wire_message["tool_calls"]] == [
            '{"command":"echo one"}',
            '{"command":"echo two"}',
        ]
