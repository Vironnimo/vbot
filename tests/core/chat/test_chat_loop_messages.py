"""Chat-loop tests grouped by messages."""

from __future__ import annotations

import json
from typing import Any, cast

from core.chat import (
    ChatMessage,
    ToolCall,
)
from core.chat.streaming import StreamingChunkTimeoutError
from core.providers.errors import (
    NetworkError,
)
from core.tools import (
    tool_success,
)

JsonObject = dict[str, Any]


class TestEmbedNotesIntoRequest:
    def test_defers_note_between_assistant_tool_calls_and_tool_result(self) -> None:
        from core.chat.chat import _embed_notes_into_request

        messages = [
            ChatMessage.user("Use the tool"),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[ToolCall(id="call_1", name="record_note", arguments={})],
            ),
            ChatMessage.note("Tool finished background work"),
            ChatMessage.tool(
                tool_call_id="call_1",
                name="record_note",
                content=json.dumps(tool_success({"ok": True})),
            ),
        ]

        request = _embed_notes_into_request(messages)

        assert [message["role"] for message in request] == ["user", "assistant", "tool", "user"]
        assert request[-1] == {
            "role": "user",
            "content": "<system-reminder>\nTool finished background work\n</system-reminder>",
        }

    def test_defers_multiple_notes_within_one_tool_sequence(self) -> None:
        from core.chat.chat import _embed_notes_into_request

        messages = [
            ChatMessage.user("Use tools"),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[
                    ToolCall(id="call_1", name="record_note", arguments={}),
                    ToolCall(id="call_2", name="record_note", arguments={}),
                ],
            ),
            ChatMessage.note("First note"),
            ChatMessage.tool(
                tool_call_id="call_1",
                name="record_note",
                content=json.dumps(tool_success({"ok": True})),
            ),
            ChatMessage.note("Second note"),
            ChatMessage.tool(
                tool_call_id="call_2",
                name="record_note",
                content=json.dumps(tool_success({"ok": True})),
            ),
        ]

        request = _embed_notes_into_request(messages)

        assert [message["role"] for message in request] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "user",
        ]
        assert request[-1] == {
            "role": "user",
            "content": (
                "<system-reminder>\nFirst note\n</system-reminder>\n"
                "<system-reminder>\nSecond note\n</system-reminder>"
            ),
        }

    def test_note_between_two_tool_sequences_is_not_deferred(self) -> None:
        from core.chat.chat import _embed_notes_into_request

        messages = [
            ChatMessage.user("Start"),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[ToolCall(id="call_1", name="record_note", arguments={})],
            ),
            ChatMessage.tool(
                tool_call_id="call_1",
                name="record_note",
                content=json.dumps(tool_success({"ok": True})),
            ),
            ChatMessage.note("Between sequences"),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[ToolCall(id="call_2", name="record_note", arguments={})],
            ),
            ChatMessage.tool(
                tool_call_id="call_2",
                name="record_note",
                content=json.dumps(tool_success({"ok": True})),
            ),
        ]

        request = _embed_notes_into_request(messages)

        assert [message["role"] for message in request] == [
            "user",
            "assistant",
            "tool",
            "user",
            "assistant",
            "tool",
        ]
        assert request[3] == {
            "role": "user",
            "content": "<system-reminder>\nBetween sequences\n</system-reminder>",
        }

    def test_notes_before_tool_sequence_emit_before_assistant_message(self) -> None:
        from core.chat.chat import _embed_notes_into_request

        messages = [
            ChatMessage.note("Pre-sequence note"),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[ToolCall(id="call_1", name="record_note", arguments={})],
            ),
            ChatMessage.tool(
                tool_call_id="call_1",
                name="record_note",
                content=json.dumps(tool_success({"ok": True})),
            ),
        ]

        request = _embed_notes_into_request(messages)

        assert [message["role"] for message in request] == ["user", "assistant", "tool"]
        assert request[0] == {
            "role": "user",
            "content": "<system-reminder>\nPre-sequence note\n</system-reminder>",
        }

    def test_skips_reasoning_only_assistant_message(self) -> None:
        from core.chat.chat import _embed_notes_into_request

        messages = [
            ChatMessage.user("Previous question"),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                reasoning="Old reasoning",
            ),
            ChatMessage.user("Follow up"),
        ]

        request = _embed_notes_into_request(messages)

        assert [message["role"] for message in request] == ["user", "user"]
        assert request[0]["content"] == "Previous question"
        assert request[1]["content"] == "Follow up"


class TestMessageToRequestDict:
    """Verify _message_to_request_dict strips assistant-only history metadata."""

    def test_strips_reasoning_reasoning_meta_and_usage_from_assistant_message(self):
        """Old assistant reasoning fields must not be resent on fresh follow-up turns."""
        from core.chat.chat import _message_to_request_dict

        message = ChatMessage.assistant(
            model="openai/gpt-4",
            content="Hello",
            reasoning="Need context before reply.",
            reasoning_meta={"opaque": "provider-signed"},
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        result = _message_to_request_dict(message)

        assert "usage" not in result
        assert "reasoning" not in result
        assert "reasoning_meta" not in result
        assert result["content"] == "Hello"

    def test_opencode_adapter_maps_reasoning_content_when_current_turn_payload_includes_it(self):
        """Current-turn assistant payloads still map reasoning to reasoning_content."""
        from core.providers.opencode_go import OpenCodeGoAdapter

        with_reasoning = ChatMessage.assistant(
            model="opencode-go/deepseek-v4-pro",
            content="Answer.",
            reasoning="Need to inspect prior tool output.",
            reasoning_meta={"opaque": "signed"},
        ).to_dict()
        without_reasoning = ChatMessage.assistant(
            model="opencode-go/deepseek-v4-pro",
            content="Answer without explicit reasoning.",
        ).to_dict()

        adapter = cast(OpenCodeGoAdapter, object.__new__(OpenCodeGoAdapter))
        formatted_with_reasoning = adapter._format_assistant_message(with_reasoning)
        formatted_without_reasoning = adapter._format_assistant_message(without_reasoning)

        assert formatted_with_reasoning["reasoning_content"] == "Need to inspect prior tool output."
        assert "reasoning_content" not in formatted_without_reasoning

    def test_request_dict_strips_reasoning_before_adapter_history_formatting(self):
        """History conversion should remove reasoning before adapter formatting runs."""
        from core.chat.chat import _message_to_request_dict
        from core.providers.opencode_go import OpenCodeGoAdapter

        assistant_history_message = ChatMessage.assistant(
            model="opencode-go/deepseek-v4-pro",
            content="Old answer.",
            reasoning="Old reasoning that must not be resent.",
            reasoning_meta={"opaque": "signed"},
        )
        request_history_message = _message_to_request_dict(assistant_history_message)
        assert "reasoning" not in request_history_message
        assert "reasoning_meta" not in request_history_message

        adapter = cast(OpenCodeGoAdapter, object.__new__(OpenCodeGoAdapter))
        formatted_history_message = adapter._format_assistant_message(request_history_message)

        assert "reasoning_content" not in formatted_history_message

    def test_preserves_usage_on_non_assistant_messages(self):
        """User and tool messages never have usage, but the function should not strip it."""
        from core.chat.chat import _message_to_request_dict

        message = ChatMessage.user("What is the weather?")
        result = _message_to_request_dict(message)

        assert "usage" not in result
        assert result["content"] == "What is the weather?"

    def test_strips_timing_from_tool_messages(self):
        from core.chat.chat import _message_to_request_dict

        message = ChatMessage.tool(
            tool_call_id="call-one",
            name="read",
            content='{"ok":true,"error":null,"data":{},"artifacts":[]}',
            timing={
                "started_at": "2026-05-03T14:30:01+00:00",
                "completed_at": "2026-05-03T14:30:02+00:00",
                "duration_ms": 1000,
            },
        )

        result = _message_to_request_dict(message)

        assert result["role"] == "tool"
        assert "timing" not in result

    def test_run_summary_is_omitted_from_request_history(self):
        from core.chat.chat import _embed_notes_into_request

        messages = [
            ChatMessage.user("Previous question"),
            ChatMessage.assistant(model="openai/gpt-4", content="Previous answer"),
            ChatMessage.run_summary(
                run_id="run-one",
                status="completed",
                iteration_count=1,
                timing={
                    "started_at": "2026-05-03T14:30:01+00:00",
                    "completed_at": "2026-05-03T14:30:02+00:00",
                    "duration_ms": 1000,
                },
            ),
            ChatMessage.user("Follow up"),
        ]

        result = _embed_notes_into_request(messages)

        assert [message["role"] for message in result] == ["user", "assistant", "user"]
        assert all("timing" not in message for message in result)


class TestErrorKindClassification:
    def test_streaming_chunk_timeout_maps_to_timeout(self) -> None:
        from core.chat.chat import ERROR_KIND_TIMEOUT, _exception_to_error_kind

        assert _exception_to_error_kind(StreamingChunkTimeoutError("stalled")) == ERROR_KIND_TIMEOUT

    def test_network_error_maps_to_network_error_kind(self) -> None:
        from core.chat.chat import ERROR_KIND_NETWORK, _exception_to_error_kind

        assert _exception_to_error_kind(NetworkError("offline")) == ERROR_KIND_NETWORK

    def test_network_error_does_not_trigger_model_fallback(self) -> None:
        from core.chat.chat import _is_model_fallback_trigger

        assert _is_model_fallback_trigger(NetworkError("offline")) is False
