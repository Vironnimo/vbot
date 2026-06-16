"""Tests for canonical chat message primitives."""

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest

from core.chat import ChatMessage, ChatMessageValidationError, MessageSender, ToolCall
from core.chat.chat import (
    ERROR_KIND_AUTH,
    ERROR_KIND_CONFIG,
    ERROR_KIND_PROVIDER_ERROR,
    ERROR_KIND_PROVIDER_FATAL,
    ERROR_KIND_PROVIDER_OVERLOAD,
    ERROR_KIND_RATE_LIMIT,
    ERROR_KIND_TIMEOUT,
    ERROR_KIND_TOOL_ITERATIONS,
    error_kind_llm_visible,
)
from core.chat.content_blocks import FileBlock, TextBlock
from core.chat.messages import (
    INTERRUPTED_TOOL_RESULT_CODE,
    INTERRUPTED_TOOL_RESULT_MESSAGE,
    _assistant_continuation_dict,
    _embed_notes_into_request,
    _message_to_request_dict,
    _repair_dangling_tool_calls,
    _restore_in_run_assistant_reasoning,
)
from core.providers.reasoning import (
    REASONING_REPLAY_FULL_HISTORY,
    REASONING_REPLAY_NONE,
)
from core.sessions import PARTIAL_THINKING_NOTE_PREFIX

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)
FIXED_TIMING = {
    "started_at": "2026-05-03T14:30:01+00:00",
    "completed_at": "2026-05-03T14:30:02+00:00",
    "duration_ms": 1234,
}


class TestToolCall:
    def test_to_dict_returns_canonical_fields(self):
        tool_call = ToolCall(id="call_abc", name="get_weather", arguments={"city": "Berlin"})

        assert tool_call.to_dict() == {
            "id": "call_abc",
            "name": "get_weather",
            "arguments": {"city": "Berlin"},
        }

    def test_from_dict_rejects_non_object_arguments(self):
        with pytest.raises(ChatMessageValidationError, match="arguments"):
            ToolCall.from_dict({"id": "call_abc", "name": "get_weather", "arguments": []})

    def test_frozen(self):
        tool_call = ToolCall(id="call_abc", name="get_weather")

        with pytest.raises(FrozenInstanceError):
            tool_call.name = "changed"  # type: ignore[misc]


class TestMessageSender:
    def test_to_dict_returns_canonical_fields(self):
        sender = MessageSender(id="50", display_name="Alice")

        assert sender.to_dict() == {"id": "50", "display_name": "Alice"}

    def test_from_dict_round_trips(self):
        sender = MessageSender(id="50", display_name="Alice")

        assert MessageSender.from_dict(sender.to_dict()) == sender

    @pytest.mark.parametrize("bad_id", [None, "", 50, {"nested": True}])
    def test_from_dict_rejects_bad_id(self, bad_id):
        with pytest.raises(
            ChatMessageValidationError, match="sender id must be a non-empty string"
        ):
            MessageSender.from_dict({"id": bad_id, "display_name": "Alice"})

    @pytest.mark.parametrize("bad_display_name", [None, "", 50, ["Alice"]])
    def test_from_dict_rejects_bad_display_name(self, bad_display_name):
        with pytest.raises(
            ChatMessageValidationError,
            match="sender display_name must be a non-empty string",
        ):
            MessageSender.from_dict({"id": "50", "display_name": bad_display_name})

    def test_frozen(self):
        sender = MessageSender(id="50", display_name="Alice")

        with pytest.raises(FrozenInstanceError):
            sender.display_name = "changed"  # type: ignore[misc]


class TestChatMessageFactories:
    def test_system_message_contains_required_model_and_content(self):
        message = ChatMessage.system(
            "You are an agent for vBot.",
            "anthropic/claude-sonnet-4",
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "system",
            "model": "anthropic/claude-sonnet-4",
            "content": "You are an agent for vBot.",
        }

    def test_user_message_omits_model(self):
        message = ChatMessage.user("What's the weather in Berlin?", timestamp=FIXED_TIMESTAMP)

        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "user",
            "content": "What's the weather in Berlin?",
        }

    def test_user_message_with_sender_round_trips(self):
        sender = MessageSender(id="50", display_name="Alice")

        message = ChatMessage.user(
            "Hello from the group.", sender=sender, timestamp=FIXED_TIMESTAMP
        )

        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "user",
            "content": "Hello from the group.",
            "sender": {"id": "50", "display_name": "Alice"},
        }

        parsed = ChatMessage.from_dict(message.to_dict())
        assert parsed.sender == sender
        assert parsed.content == "Hello from the group."

    def test_user_message_without_sender_omits_sender_key(self):
        message = ChatMessage.user("Hello", timestamp=FIXED_TIMESTAMP)

        assert "sender" not in message.to_dict()
        assert message.sender is None

    def test_user_message_round_trips_content_block_list(self):
        blocks = [
            TextBlock(type="text", text="Please review the document."),
            FileBlock(
                type="file",
                attachment_id="att_123",
                filename="report.pdf",
                media_type="application/pdf",
            ),
        ]

        message = ChatMessage.user(blocks, timestamp=FIXED_TIMESTAMP)

        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "user",
            "content": [
                {"type": "text", "text": "Please review the document."},
                {
                    "type": "file",
                    "attachment_id": "att_123",
                    "filename": "report.pdf",
                    "media_type": "application/pdf",
                },
            ],
        }

        parsed = ChatMessage.from_dict(message.to_dict())
        assert parsed.content == blocks

    def test_note_message_contains_only_content(self):
        message = ChatMessage.note("Background task completed.", timestamp=FIXED_TIMESTAMP)

        assert message.role == "note"
        assert message.content == "Background task completed."
        assert message.model is None
        assert message.reasoning is None
        assert message.reasoning_meta is None
        assert message.usage is None
        assert message.tool_calls is None
        assert message.tool_call_id is None
        assert message.name is None
        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "note",
            "content": "Background task completed.",
        }

    def test_error_message_contains_error_kind_and_content(self):
        message = ChatMessage.error(
            ERROR_KIND_RATE_LIMIT,
            "Provider rate limit exceeded.",
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.role == "error"
        assert message.content == "Provider rate limit exceeded."
        assert message.error_kind == ERROR_KIND_RATE_LIMIT
        assert message.model is None
        assert message.reasoning is None
        assert message.reasoning_meta is None
        assert message.usage is None
        assert message.tool_calls is None
        assert message.tool_call_id is None
        assert message.name is None
        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "error",
            "content": "Provider rate limit exceeded.",
            "error_kind": "rate_limit",
        }

    def test_assistant_message_preserves_reasoning_meta_and_tool_calls(self):
        tool_call = ToolCall(id="call_abc", name="get_weather", arguments={"city": "Berlin"})
        message = ChatMessage.assistant(
            model="anthropic/claude-sonnet-4",
            content=None,
            reasoning="I need to call the weather tool.",
            reasoning_meta={"signature": "opaque"},
            tool_calls=[tool_call],
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "assistant",
            "model": "anthropic/claude-sonnet-4",
            "reasoning": "I need to call the weather tool.",
            "reasoning_meta": {"signature": "opaque"},
            "tool_calls": [
                {"id": "call_abc", "name": "get_weather", "arguments": {"city": "Berlin"}},
            ],
        }

    def test_tool_message_contains_tool_correlation_fields(self):
        message = ChatMessage.tool(
            tool_call_id="call_abc",
            name="get_weather",
            content='{"temp":22,"condition":"sunny"}',
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "tool",
            "content": '{"temp":22,"condition":"sunny"}',
            "tool_call_id": "call_abc",
            "name": "get_weather",
        }

    def test_tool_message_preserves_timing(self):
        message = ChatMessage.tool(
            tool_call_id="call_abc",
            name="get_weather",
            content='{"temp":22}',
            timing=FIXED_TIMING,
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.timing == FIXED_TIMING
        assert message.to_dict()["timing"] == FIXED_TIMING

    def test_run_summary_contains_run_status_and_timing(self):
        message = ChatMessage.run_summary(
            run_id="run-one",
            status="completed",
            timing=FIXED_TIMING,
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "run_summary",
            "timing": FIXED_TIMING,
            "run_id": "run-one",
            "status": "completed",
        }

    def test_assistant_message_with_usage(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="The answer is 42.",
            usage={"input_tokens": 150, "output_tokens": 12},
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.usage == {"input_tokens": 150, "output_tokens": 12}
        result = message.to_dict()
        assert result["usage"] == {"input_tokens": 150, "output_tokens": 12}

    def test_assistant_message_without_usage_defaults_to_none(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="The answer is 42.",
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.usage is None
        result = message.to_dict()
        assert "usage" not in result

    def test_assistant_message_interrupted_round_trips(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="Partial answer",
            interrupted=True,
            timestamp=FIXED_TIMESTAMP,
        )

        result = message.to_dict()
        assert result["interrupted"] is True
        assert ChatMessage.from_dict(result).interrupted is True

    def test_assistant_message_not_interrupted_omits_flag(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="Complete answer",
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.interrupted is False
        assert "interrupted" not in message.to_dict()

    def test_interrupted_rejected_on_non_assistant_role(self):
        with pytest.raises(ChatMessageValidationError, match="interrupted"):
            ChatMessage.from_dict(
                {
                    "id": "u1",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "user",
                    "content": "hi",
                    "interrupted": True,
                }
            )

    def test_interrupted_must_be_boolean(self):
        with pytest.raises(ChatMessageValidationError, match="interrupted must be a boolean"):
            ChatMessage.from_dict(
                {
                    "id": "a1",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "assistant",
                    "model": "openai/gpt-4.1",
                    "content": "hi",
                    "interrupted": "yes",
                }
            )

    def test_naive_timestamp_is_rejected(self):
        with pytest.raises(ChatMessageValidationError, match="timezone"):
            ChatMessage.user("hello", timestamp=datetime(2026, 5, 3, 14, 30))


class TestChatMessageParsing:
    def test_from_dict_deserializes_user_content_block_list(self):
        data = {
            "id": "msg_blocks_1",
            "timestamp": "2026-05-03T14:30:01+00:00",
            "role": "user",
            "content": [
                {"type": "text", "text": "Please read this."},
                {
                    "type": "file",
                    "attachment_id": "att_123",
                    "filename": "report.pdf",
                    "media_type": "application/pdf",
                },
            ],
        }

        message = ChatMessage.from_dict(data)

        assert message.content == [
            TextBlock(type="text", text="Please read this."),
            FileBlock(
                type="file",
                attachment_id="att_123",
                filename="report.pdf",
                media_type="application/pdf",
            ),
        ]
        assert message.to_dict() == data

    def test_from_dict_round_trips_assistant_message(self):
        data = {
            "id": "g7h8i9",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "assistant",
            "model": "anthropic/claude-sonnet-4",
            "content": "The weather is sunny.",
            "reasoning_meta": {"signature": "opaque"},
        }

        message = ChatMessage.from_dict(data)

        assert message.to_dict() == data

    def test_from_dict_round_trips_run_summary(self):
        data = {
            "id": "summary-one",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "run_summary",
            "run_id": "run-one",
            "status": "completed",
            "timing": FIXED_TIMING,
        }

        message = ChatMessage.from_dict(data)

        assert message.to_dict() == data

    def test_from_dict_rejects_bad_timing_duration(self):
        with pytest.raises(ChatMessageValidationError, match="duration_ms"):
            ChatMessage.from_dict(
                {
                    "id": "summary-one",
                    "timestamp": "2026-05-03T14:30:05+00:00",
                    "role": "run_summary",
                    "run_id": "run-one",
                    "status": "completed",
                    "timing": {
                        "started_at": "2026-05-03T14:30:01+00:00",
                        "completed_at": "2026-05-03T14:30:02+00:00",
                        "duration_ms": -1,
                    },
                }
            )

    def test_from_dict_accepts_z_utc_timestamp(self):
        data = {
            "id": "d4e5f6",
            "timestamp": "2026-05-03T14:30:01Z",
            "role": "user",
            "content": "Hello",
        }

        message = ChatMessage.from_dict(data)

        assert message.to_dict() == data

    def test_from_dict_round_trips_note_message(self):
        data = {
            "id": "note_abc",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "role": "note",
            "content": "Background task completed.",
        }

        message = ChatMessage.from_dict(data)

        assert message.role == "note"
        assert message.content == "Background task completed."
        assert message.to_dict() == data

    def test_from_dict_round_trips_error_message(self):
        data = {
            "id": "error_abc",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "role": "error",
            "content": "Provider timed out.",
            "error_kind": "timeout",
        }

        message = ChatMessage.from_dict(data)

        assert message.role == "error"
        assert message.content == "Provider timed out."
        assert message.error_kind == "timeout"
        assert message.to_dict() == data

    def test_from_dict_round_trips_unknown_error_kind(self):
        data = {
            "id": "error_unknown",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "role": "error",
            "content": "Future error kind.",
            "error_kind": "future_kind",
        }

        message = ChatMessage.from_dict(data)

        assert message.error_kind == "future_kind"
        assert message.to_dict() == data

    def test_unknown_extra_fields_are_ignored(self):
        data = {
            "id": "d4e5f6",
            "timestamp": "2026-05-03T14:30:01+00:00",
            "role": "user",
            "content": "Hello",
            "future_field": "ignored",
        }

        message = ChatMessage.from_dict(data)

        assert "future_field" not in message.to_dict()

    def test_invalid_role_is_rejected(self):
        with pytest.raises(ChatMessageValidationError, match="role"):
            ChatMessage.from_dict(
                {
                    "id": "d4e5f6",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "developer",
                    "content": "Hello",
                }
            )

    def test_user_message_rejects_model(self):
        with pytest.raises(ChatMessageValidationError, match="model"):
            ChatMessage.from_dict(
                {
                    "id": "d4e5f6",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "user",
                    "model": "openai/gpt-5.2",
                    "content": "Hello",
                }
            )

    def test_user_message_rejects_empty_content_block_list(self):
        with pytest.raises(ChatMessageValidationError, match="must not be empty"):
            ChatMessage.from_dict(
                {
                    "id": "msg_empty_blocks",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "user",
                    "content": [],
                }
            )

    @pytest.mark.parametrize(
        ("role", "extra_fields"),
        [
            ("system", {"model": "openai/gpt-4.1"}),
            ("assistant", {"model": "openai/gpt-4.1"}),
            ("tool", {"tool_call_id": "call_abc", "name": "get_weather"}),
            ("note", {}),
            ("error", {"error_kind": "provider_error"}),
        ],
    )
    def test_non_user_messages_reject_content_block_list(self, role, extra_fields):
        data = {
            "id": f"msg_blocks_{role}",
            "timestamp": "2026-05-03T14:30:01+00:00",
            "role": role,
            "content": [{"type": "text", "text": "Hello"}],
        }
        data.update(extra_fields)

        with pytest.raises(ChatMessageValidationError, match="content"):
            ChatMessage.from_dict(data)

    def test_tool_message_requires_tool_call_id(self):
        with pytest.raises(ChatMessageValidationError, match="tool_call_id"):
            ChatMessage.from_dict(
                {
                    "id": "j0k1l2",
                    "timestamp": "2026-05-03T14:30:06+00:00",
                    "role": "tool",
                    "name": "get_weather",
                    "content": "{}",
                }
            )

    def test_from_dict_reads_usage_on_assistant_message(self):
        data = {
            "id": "msg_usage_1",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "assistant",
            "model": "openai/gpt-4.1",
            "content": "Result.",
            "usage": {"input_tokens": 200, "output_tokens": 30},
        }

        message = ChatMessage.from_dict(data)

        assert message.usage == {"input_tokens": 200, "output_tokens": 30}
        assert message.to_dict() == data

    def test_from_dict_omits_usage_when_absent(self):
        data = {
            "id": "msg_no_usage",
            "timestamp": "2026-05-03T14:30:05+00:00",
            "role": "assistant",
            "model": "openai/gpt-4.1",
            "content": "Result.",
        }

        message = ChatMessage.from_dict(data)

        assert message.usage is None
        assert "usage" not in message.to_dict()

    def test_from_dict_rejects_non_object_usage(self):
        with pytest.raises(ChatMessageValidationError, match="usage must be an object"):
            ChatMessage.from_dict(
                {
                    "id": "msg_bad_usage",
                    "timestamp": "2026-05-03T14:30:05+00:00",
                    "role": "assistant",
                    "model": "openai/gpt-4.1",
                    "content": "Result.",
                    "usage": "not a dict",
                }
            )

    def test_from_dict_rejects_usage_on_user_message(self):
        with pytest.raises(ChatMessageValidationError, match="usage"):
            ChatMessage.from_dict(
                {
                    "id": "msg_usage_user",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "user",
                    "content": "Hello",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                }
            )

    def test_from_dict_rejects_usage_on_system_message(self):
        with pytest.raises(ChatMessageValidationError, match="usage"):
            ChatMessage.from_dict(
                {
                    "id": "msg_usage_sys",
                    "timestamp": "2026-05-03T14:30:00+00:00",
                    "role": "system",
                    "model": "openai/gpt-4.1",
                    "content": "You are helpful.",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                }
            )

    def test_from_dict_rejects_usage_on_tool_message(self):
        with pytest.raises(ChatMessageValidationError, match="usage"):
            ChatMessage.from_dict(
                {
                    "id": "msg_usage_tool",
                    "timestamp": "2026-05-03T14:30:06+00:00",
                    "role": "tool",
                    "tool_call_id": "call_abc",
                    "name": "get_weather",
                    "content": "{}",
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                }
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("model", "openai/gpt-4.1"),
            ("reasoning", "thinking"),
            ("reasoning_meta", {"signature": "opaque"}),
            ("usage", {"input_tokens": 10, "output_tokens": 0}),
            ("tool_calls", [{"id": "call_abc", "name": "get_weather", "arguments": {}}]),
            ("tool_call_id", "call_abc"),
            ("name", "get_weather"),
        ],
    )
    def test_from_dict_rejects_optional_fields_on_note_message(self, field, value):
        data = {
            "id": "note_bad",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "role": "note",
            "content": "Background task completed.",
            field: value,
        }

        with pytest.raises(ChatMessageValidationError, match=field):
            ChatMessage.from_dict(data)

    def test_from_dict_rejects_note_without_content(self):
        with pytest.raises(ChatMessageValidationError, match="content"):
            ChatMessage.from_dict(
                {
                    "id": "note_missing_content",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "role": "note",
                }
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("model", "openai/gpt-4.1"),
            ("reasoning", "thinking"),
            ("reasoning_meta", {"signature": "opaque"}),
            ("usage", {"input_tokens": 10, "output_tokens": 0}),
            ("tool_calls", [{"id": "call_abc", "name": "get_weather", "arguments": {}}]),
            ("tool_call_id", "call_abc"),
            ("name", "get_weather"),
        ],
    )
    def test_from_dict_rejects_optional_fields_on_error_message(self, field, value):
        data = {
            "id": "error_bad",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "role": "error",
            "content": "Provider failed.",
            "error_kind": "provider_error",
            field: value,
        }

        with pytest.raises(ChatMessageValidationError, match=field):
            ChatMessage.from_dict(data)

    def test_from_dict_rejects_error_without_content(self):
        with pytest.raises(ChatMessageValidationError, match="content"):
            ChatMessage.from_dict(
                {
                    "id": "error_missing_content",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "role": "error",
                    "error_kind": "provider_error",
                }
            )

    def test_from_dict_rejects_error_without_error_kind(self):
        with pytest.raises(ChatMessageValidationError, match="error_kind"):
            ChatMessage.from_dict(
                {
                    "id": "error_missing_kind",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "role": "error",
                    "content": "Provider failed.",
                }
            )

    def test_from_dict_rejects_error_with_empty_error_kind(self):
        with pytest.raises(ChatMessageValidationError, match="error_kind"):
            ChatMessage.from_dict(
                {
                    "id": "error_empty_kind",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "role": "error",
                    "content": "Provider failed.",
                    "error_kind": "",
                }
            )

    def test_from_dict_rejects_non_object_sender(self):
        with pytest.raises(ChatMessageValidationError, match="sender must be an object"):
            ChatMessage.from_dict(
                {
                    "id": "msg_bad_sender",
                    "timestamp": "2026-05-03T14:30:00+00:00",
                    "role": "user",
                    "content": "Hello",
                    "sender": "Alice|50",
                }
            )

    def test_from_dict_rejects_malformed_sender_object(self):
        with pytest.raises(
            ChatMessageValidationError, match="sender id must be a non-empty string"
        ):
            ChatMessage.from_dict(
                {
                    "id": "msg_malformed_sender",
                    "timestamp": "2026-05-03T14:30:00+00:00",
                    "role": "user",
                    "content": "Hello",
                    "sender": {"display_name": "Alice"},
                }
            )

    @pytest.mark.parametrize(
        ("role", "extra_fields"),
        [
            ("system", {"model": "openai/gpt-4.1", "content": "You are helpful."}),
            ("assistant", {"model": "openai/gpt-4.1", "content": "Answer."}),
            (
                "tool",
                {"tool_call_id": "call_abc", "name": "get_weather", "content": "{}"},
            ),
            ("note", {"content": "Background task completed."}),
            ("error", {"content": "Provider failed.", "error_kind": "provider_error"}),
            (
                "compaction_checkpoint",
                {"content": "Summary.", "tail_boundary_id": "msg_tail"},
            ),
            (
                "run_summary",
                {"run_id": "run-one", "status": "completed", "timing": FIXED_TIMING},
            ),
        ],
    )
    def test_from_dict_rejects_sender_on_non_user_roles(self, role, extra_fields):
        data = {
            "id": f"msg_sender_{role}",
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": role,
            "sender": {"id": "50", "display_name": "Alice"},
            **extra_fields,
        }

        with pytest.raises(ChatMessageValidationError, match="sender"):
            ChatMessage.from_dict(data)

    def test_from_dict_usage_as_array_is_rejected(self):
        with pytest.raises(ChatMessageValidationError, match="usage must be an object"):
            ChatMessage.from_dict(
                {
                    "id": "msg_usage_arr",
                    "timestamp": "2026-05-03T14:30:05+00:00",
                    "role": "assistant",
                    "model": "openai/gpt-4.1",
                    "content": "Result.",
                    "usage": [1, 2, 3],
                }
            )


class TestSenderRequestRendering:
    """Sender attribution exists only in provider requests, never in persisted content."""

    def test_string_content_gets_attribution_prefix(self):
        message = ChatMessage.user(
            "What's the plan?",
            sender=MessageSender(id="50", display_name="Alice"),
        )

        result = _message_to_request_dict(message)

        assert result["content"] == "[Alice|50]: What's the plan?"
        assert "sender" not in result

    def test_block_content_gets_leading_attribution_text_block(self):
        blocks = [
            TextBlock(type="text", text="Please review."),
            FileBlock(
                type="file",
                attachment_id="att_123",
                filename="report.pdf",
                media_type="application/pdf",
            ),
        ]
        message = ChatMessage.user(blocks, sender=MessageSender(id="50", display_name="Alice"))

        result = _message_to_request_dict(message)

        assert result["content"][0] == {"type": "text", "text": "[Alice|50]:"}
        assert result["content"][1] == {"type": "text", "text": "Please review."}
        assert len(result["content"]) == 3
        assert "sender" not in result

    def test_user_message_without_sender_is_unchanged(self):
        message = ChatMessage.user("What's the plan?")

        result = _message_to_request_dict(message)

        assert result["content"] == "What's the plan?"
        assert "sender" not in result

    def test_persisted_content_stays_clean(self):
        message = ChatMessage.user(
            "What's the plan?",
            sender=MessageSender(id="50", display_name="Alice"),
        )

        _message_to_request_dict(message)

        assert message.content == "What's the plan?"
        assert message.to_dict()["content"] == "What's the plan?"

    def test_tag_parts_are_sanitized_against_spoofing(self):
        message = ChatMessage.user(
            "Hi",
            sender=MessageSender(id="5|0", display_name="[Bob|99]: fake\r\nname"),
        )

        result = _message_to_request_dict(message)

        assert result["content"] == "[Bob99: fakename|50]: Hi"

    def test_tag_part_empty_after_sanitizing_falls_back_to_unknown(self):
        message = ChatMessage.user(
            "Hi",
            sender=MessageSender(id="[]|", display_name="|||"),
        )

        result = _message_to_request_dict(message)

        assert result["content"] == "[unknown|unknown]: Hi"


class TestErrorKindLlmVisibility:
    @pytest.mark.parametrize(
        "kind",
        [
            ERROR_KIND_RATE_LIMIT,
            ERROR_KIND_TIMEOUT,
            ERROR_KIND_PROVIDER_OVERLOAD,
            ERROR_KIND_TOOL_ITERATIONS,
            ERROR_KIND_PROVIDER_ERROR,
        ],
    )
    def test_llm_visible_error_kinds_return_true(self, kind):
        assert error_kind_llm_visible(kind) is True

    @pytest.mark.parametrize(
        "kind",
        [
            ERROR_KIND_AUTH,
            ERROR_KIND_PROVIDER_FATAL,
            ERROR_KIND_CONFIG,
            "future_kind",
        ],
    )
    def test_llm_invisible_and_unknown_error_kinds_return_false(self, kind):
        assert error_kind_llm_visible(kind) is False


def _synthesized_failure_envelope() -> dict:
    return {
        "ok": False,
        "error": {
            "code": INTERRUPTED_TOOL_RESULT_CODE,
            "message": INTERRUPTED_TOOL_RESULT_MESSAGE,
        },
        "data": None,
        "artifacts": [],
    }


class TestRepairDanglingToolCalls:
    """The shared history-build path must synthesize tool results for dangling tool_calls."""

    def test_dangling_assistant_followed_by_error_synthesizes_tool_results(self) -> None:
        # Arrange: a history broken by the bug-hunt repro — an assistant turn
        # with tool_calls persisted, but no tool results, followed by an error.
        messages = [
            ChatMessage.user("Do something", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[
                    ToolCall(id="call_one", name="read", arguments={"path": "x"}),
                    ToolCall(id="call_two", name="read", arguments={"path": "y"}),
                ],
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.error(
                ERROR_KIND_PROVIDER_ERROR,
                "Run aborted.",
                timestamp=FIXED_TIMESTAMP,
            ),
        ]

        # Act
        request = _embed_notes_into_request(messages)

        # Assert: synthesized tool results come immediately after the assistant
        # turn, followed by the LLM-visible error as a system-reminder note.
        assert [message["role"] for message in request] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "user",
        ]
        for entry, expected_id in zip(request[2:4], ["call_one", "call_two"], strict=True):
            assert entry["role"] == "tool"
            assert entry["tool_call_id"] == expected_id
            assert entry["name"] == "read"
            envelope = json.loads(entry["content"])
            assert envelope == _synthesized_failure_envelope()
        assert request[-1]["role"] == "user"
        assert "Run aborted." in request[-1]["content"]

    def test_partial_results_only_synthesizes_missing_call_preserves_order(self) -> None:
        # Arrange: 2 of 3 sibling tool calls were persisted; the missing one
        # must be synthesized and the existing two kept. Synthesized entries
        # appear in the assistant's original tool-call order relative to each
        # other (this is the only order the repair can establish without
        # re-ordering the existing persisted tool entries).
        messages = [
            ChatMessage.user("Multi", timestamp=FIXED_TIMESTAMP),
            ChatMessage.assistant(
                model="openai/gpt-5.2",
                content=None,
                tool_calls=[
                    ToolCall(id="call_alpha", name="read", arguments={}),
                    ToolCall(id="call_beta", name="read", arguments={}),
                    ToolCall(id="call_gamma", name="read", arguments={}),
                ],
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.tool(
                tool_call_id="call_alpha",
                name="read",
                content=json.dumps({"ok": True, "error": None, "data": {}, "artifacts": []}),
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.tool(
                tool_call_id="call_gamma",
                name="read",
                content=json.dumps({"ok": True, "error": None, "data": {}, "artifacts": []}),
                timestamp=FIXED_TIMESTAMP,
            ),
            ChatMessage.user("Next request", timestamp=FIXED_TIMESTAMP),
        ]

        # Act
        request = _embed_notes_into_request(messages)

        # Assert: every tool_call_id is answered, exactly one synthetic entry
        # is added, and the synthetic one is the missing call (beta).
        assert [message["role"] for message in request] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "tool",
            "user",
        ]
        answered = [entry.get("tool_call_id") for entry in request if entry.get("role") == "tool"]
        assert sorted(answered) == ["call_alpha", "call_beta", "call_gamma"]  # type: ignore[type-var]
        synthetic_ids = [
            entry.get("tool_call_id")
            for entry in request
            if entry.get("role") == "tool" and "result_unavailable" in entry.get("content", "")
        ]
        assert synthetic_ids == ["call_beta"]
        # The synthesized entry must come after the dangling assistant turn;
        # the trailing user message stays last.
        assert request[-1]["role"] == "user"
        assert request[-1]["content"] == "Next request"

    def test_compaction_tail_path_gets_same_repair(self, tmp_path) -> None:
        # Arrange: tail of a compacted session contains a dangling assistant turn.
        from core.chat.chat import ChatLoop
        from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

        agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
        runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
        session = runtime.chat_sessions.create("coder", session_id="session-one")

        tail_user = ChatMessage.user("Current question", timestamp=FIXED_TIMESTAMP)
        session.append(ChatMessage.user("Old question", timestamp=FIXED_TIMESTAMP))
        session.append(ChatMessage.assistant(model=agent.model, content="Old answer"))
        session.append(tail_user)
        session.append(
            ChatMessage.assistant(
                model=agent.model,
                content=None,
                tool_calls=[ToolCall(id="dangling", name="read", arguments={})],
            )
        )
        session.append(
            ChatMessage.compaction_checkpoint(
                summary="Compacted earlier turns.",
                tail_boundary_id=tail_user.id,
                compacted_token_count=10,
            )
        )

        # Act: build the compacted request history through the same path the
        # chat loop uses (which calls _embed_notes_into_request internally).
        request_messages = asyncio.run(ChatLoop(runtime)._build_request_messages(agent, session))

        # Assert: dangling tool call is answered with a synthesized failure.
        tool_entries = [entry for entry in request_messages if entry.get("role") == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0]["tool_call_id"] == "dangling"
        assert tool_entries[0]["name"] == "read"
        envelope = json.loads(tool_entries[0]["content"])
        assert envelope == _synthesized_failure_envelope()

    def test_compaction_build_recovers_from_missing_tail_boundary(self, tmp_path) -> None:
        # A checkpoint points at a tail boundary that no longer exists in
        # history (e.g. a corrupted/partial write). The build path must recover
        # instead of failing every request: keep the summary, replay
        # post-checkpoint history, and flag the loss in the summary block.
        from core.chat.chat import ChatLoop
        from core.chat.messages import COMPACTION_TAIL_RECOVERED_HINT
        from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

        agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
        runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
        session = runtime.chat_sessions.create("coder", session_id="session-one")

        # Pre-checkpoint slice whose anchor is lost, the checkpoint, then a
        # genuinely new turn appended after the checkpoint.
        session.append(ChatMessage.user("Lost tail question", timestamp=FIXED_TIMESTAMP))
        session.append(ChatMessage.assistant(model=agent.model, content="Lost tail answer"))
        session.append(
            ChatMessage.compaction_checkpoint(
                summary="Compacted earlier turns.",
                tail_boundary_id="boundary-that-no-longer-exists",
                compacted_token_count=10,
            )
        )
        session.append(ChatMessage.user("Fresh question", timestamp=FIXED_TIMESTAMP))

        request_messages = asyncio.run(ChatLoop(runtime)._build_request_messages(agent, session))

        # The summary survives and carries the recovery hint.
        summary_entries = [
            entry
            for entry in request_messages
            if entry.get("role") == "user"
            and "Compacted earlier turns." in entry.get("content", "")
        ]
        assert len(summary_entries) == 1
        assert COMPACTION_TAIL_RECOVERED_HINT in summary_entries[0]["content"]

        # Post-checkpoint history is replayed; the unanchored pre-checkpoint
        # slice is not.
        user_contents = [
            entry.get("content") for entry in request_messages if entry.get("role") == "user"
        ]
        assert any(content == "Fresh question" for content in user_contents)
        assert all("Lost tail question" not in (content or "") for content in user_contents)

    def test_repair_does_not_double_answer_already_answered_calls(self) -> None:
        # Arrange: every tool call already has a matching tool result.
        tool_envelope = json.dumps({"ok": True, "error": None, "data": {"x": 1}, "artifacts": []})
        request: list[dict[str, Any]] = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_ok", "name": "read", "arguments": {}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_ok", "name": "read", "content": tool_envelope},
        ]

        # Act
        repaired = _repair_dangling_tool_calls(request)

        # Assert: no synthetic entries are added.
        assert repaired == request

    def test_repair_preserves_synthesized_name_when_tool_call_has_name(self) -> None:
        # Arrange
        request: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "only_call", "name": "bash", "arguments": {}}],
            },
        ]

        # Act
        repaired = _repair_dangling_tool_calls(request)

        # Assert
        assert len(repaired) == 2
        assert repaired[1]["name"] == "bash"
        envelope = json.loads(repaired[1]["content"])
        assert envelope == _synthesized_failure_envelope()

    def test_repair_uses_unknown_name_when_tool_call_name_missing(self) -> None:
        # Arrange
        request: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "only_call", "arguments": {}}],
            },
        ]

        # Act
        repaired = _repair_dangling_tool_calls(request)

        # Assert
        assert repaired[1]["name"] == "unknown"
        assert repaired[1]["tool_call_id"] == "only_call"

    def test_repaired_entries_are_never_persisted_to_session_jsonl(self, tmp_path) -> None:
        # Arrange: a session with a dangling assistant turn in JSONL, then run
        # the build path. The synthesized entries must show up in the request
        # payload but not in the session file the next time we load it.
        from core.chat.chat import ChatLoop
        from tests.core.chat.test_chat_loop import StubAdapter, StubAgent, StubRuntime

        agent = StubAgent(id="coder", model="openai/gpt-5.2", allowed_tools=["*"])
        runtime: Any = StubRuntime(data_dir=tmp_path, agent=agent, adapter=StubAdapter([]))
        session = runtime.chat_sessions.create("coder", session_id="session-one")

        session.append(ChatMessage.user("Please read", timestamp=FIXED_TIMESTAMP))
        session.append(
            ChatMessage.assistant(
                model=agent.model,
                content=None,
                tool_calls=[ToolCall(id="dangling_one", name="read", arguments={})],
            )
        )
        # No tool result persisted; this is the dangling state.

        jsonl_before = session.path.read_text(encoding="utf-8")

        # Act: run the build path that synthesizes the missing tool result.
        request_messages = asyncio.run(ChatLoop(runtime)._build_request_messages(agent, session))
        jsonl_after = session.path.read_text(encoding="utf-8")

        # Assert: the request payload now contains a synthesized tool entry.
        tool_entries = [entry for entry in request_messages if entry.get("role") == "tool"]
        assert any(entry.get("tool_call_id") == "dangling_one" for entry in tool_entries)
        # Assert: the JSONL file is byte-for-byte unchanged; no synthesized
        # tool message was appended by the repair.
        assert jsonl_after == jsonl_before
        # And re-loading the session still shows the dangling assistant turn
        # (not a tool entry), confirming the repair is request-only.
        reloaded = session.load()
        assert [message.role for message in reloaded] == ["user", "assistant"]


class TestReasoningReplayShaping:
    """History shaping follows the adapter's reasoning replay policy."""

    def _assistant_with_reasoning(self, model: str, content: str | None) -> ChatMessage:
        return ChatMessage.assistant(
            model=model,
            content=content,
            reasoning="Readable thinking.",
            reasoning_meta={"content_blocks": [{"type": "thinking", "signature": "signed"}]},
            timestamp=FIXED_TIMESTAMP,
        )

    def test_full_history_keeps_reasoning_on_same_model_entries(self) -> None:
        # Arrange: persisted model carries a connection suffix; the gate must
        # compare bare models on both sides.
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            self._assistant_with_reasoning("anthropic/claude-sonnet-4::api-key", "Answer"),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4",
        )

        assert request[1]["reasoning"] == "Readable thinking."
        assert request[1]["reasoning_meta"] == {
            "content_blocks": [{"type": "thinking", "signature": "signed"}]
        }
        assert "usage" not in request[1]

    def test_full_history_strips_reasoning_on_model_mismatch(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            self._assistant_with_reasoning("openai/gpt-5.2", "Answer"),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4",
        )

        assert "reasoning" not in request[1]
        assert "reasoning_meta" not in request[1]

    def test_current_run_default_strips_reasoning_even_for_same_model(self) -> None:
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            self._assistant_with_reasoning("anthropic/claude-sonnet-4", "Answer"),
        ]

        request = _embed_notes_into_request(messages, agent_model="anthropic/claude-sonnet-4")

        assert "reasoning" not in request[1]
        assert "reasoning_meta" not in request[1]

    def test_full_history_keeps_same_model_reasoning_only_turn_in_history(self) -> None:
        # Arrange: a reasoning-only assistant turn (no content, no tool calls).
        # Same model → it survives the gate and must stay in the request;
        # mismatched model → stripped reasoning would leave it empty, so skip.
        same_model = self._assistant_with_reasoning("anthropic/claude-sonnet-4", None)
        messages = [
            ChatMessage.user("Question", timestamp=FIXED_TIMESTAMP),
            same_model,
            ChatMessage.user("Follow up", timestamp=FIXED_TIMESTAMP),
            self._assistant_with_reasoning("openai/gpt-5.2", None),
        ]

        request = _embed_notes_into_request(
            messages,
            replay_policy=REASONING_REPLAY_FULL_HISTORY,
            agent_model="anthropic/claude-sonnet-4",
        )

        assert [message["role"] for message in request] == ["user", "assistant", "user"]
        assert request[1]["id"] == same_model.id
        assert request[1]["reasoning"] == "Readable thinking."

    def test_none_policy_strips_reasoning_from_live_continuation_dict(self) -> None:
        message = self._assistant_with_reasoning("anthropic/claude-sonnet-4", "Answer")

        continuation = _assistant_continuation_dict(message, replay_policy=REASONING_REPLAY_NONE)
        default_continuation = _assistant_continuation_dict(message)

        assert "reasoning" not in continuation
        assert "reasoning_meta" not in continuation
        assert default_continuation["reasoning"] == "Readable thinking."

    def test_restore_in_run_assistant_reasoning_restores_all_matching_turns(self) -> None:
        # Arrange: the live request list carries reasoning for two in-run
        # assistant turns; the rebuilt list (post-compaction) lost both. The
        # old behavior restored only the latest tool-continuation turn.
        live_messages: list[dict[str, Any]] = [
            {
                "id": "assistant-one",
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_one", "name": "read", "arguments": {}}],
                "reasoning": "First step.",
                "reasoning_meta": {"signature": "one"},
            },
            {"id": "tool-one", "role": "tool", "tool_call_id": "call_one", "content": "{}"},
            {
                "id": "assistant-two",
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_two", "name": "read", "arguments": {}}],
                "reasoning": "Second step.",
                "reasoning_meta": {"signature": "two"},
            },
            {"id": "tool-two", "role": "tool", "tool_call_id": "call_two", "content": "{}"},
        ]
        rebuilt_messages: list[dict[str, Any]] = [
            {"role": "user", "content": "<system-reminder>\nSummary.\n</system-reminder>"},
            {
                "id": "assistant-one",
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_one", "name": "read", "arguments": {}}],
            },
            {"id": "tool-one", "role": "tool", "tool_call_id": "call_one", "content": "{}"},
            {
                "id": "assistant-two",
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_two", "name": "read", "arguments": {}}],
            },
            {"id": "tool-two", "role": "tool", "tool_call_id": "call_two", "content": "{}"},
        ]

        restored = _restore_in_run_assistant_reasoning(rebuilt_messages, live_messages)

        assert restored[1]["reasoning"] == "First step."
        assert restored[1]["reasoning_meta"] == {"signature": "one"}
        assert restored[3]["reasoning"] == "Second step."
        assert restored[3]["reasoning_meta"] == {"signature": "two"}
        assert "reasoning" not in restored[0]

    def test_restore_in_run_assistant_reasoning_skips_unmatched_entries(self) -> None:
        # Arrange: a historical assistant turn that is not part of the live
        # request list must stay stripped after the rebuild.
        live_messages: list[dict[str, Any]] = [
            {
                "id": "assistant-live",
                "role": "assistant",
                "content": "Live answer",
                "reasoning": "Live thinking.",
            },
        ]
        rebuilt_messages: list[dict[str, Any]] = [
            {"id": "assistant-old", "role": "assistant", "content": "Old answer"},
            {"id": "assistant-live", "role": "assistant", "content": "Live answer"},
        ]

        restored = _restore_in_run_assistant_reasoning(rebuilt_messages, live_messages)

        assert "reasoning" not in restored[0]
        assert restored[1]["reasoning"] == "Live thinking."


class TestPartialThinkingNoteEmbedding:
    """Phase-4 one-shot embedding of the interrupted-run partial-thinking note."""

    def _note(self, body: str) -> ChatMessage:
        return ChatMessage.note(
            f"{PARTIAL_THINKING_NOTE_PREFIX}Partial thinking before interruption:\n{body}",
            timestamp=FIXED_TIMESTAMP,
        )

    def test_embedded_when_no_assistant_turn_follows(self) -> None:
        # An interrupted run persisted only this note; the next request must
        # surface it once, with the routing prefix stripped from the text.
        messages = [
            ChatMessage.user("Do it", timestamp=FIXED_TIMESTAMP),
            self._note("half a thoug"),
            ChatMessage.user("Try again", timestamp=FIXED_TIMESTAMP),
        ]

        request = _embed_notes_into_request(messages)

        reminders = [m for m in request if "Partial thinking" in m.get("content", "")]
        assert len(reminders) == 1
        assert reminders[0]["role"] == "user"
        assert "<system-reminder>" in reminders[0]["content"]
        assert PARTIAL_THINKING_NOTE_PREFIX not in reminders[0]["content"]

    def test_skipped_once_a_later_assistant_turn_exists(self) -> None:
        # After the next run produced an assistant turn, the note is stale and
        # must no longer be embedded (it stays in JSONL for debugging).
        messages = [
            ChatMessage.user("Do it", timestamp=FIXED_TIMESTAMP),
            self._note("half a thoug"),
            ChatMessage.assistant(
                model="openai/gpt-5.2", content="done", timestamp=FIXED_TIMESTAMP
            ),
            ChatMessage.user("Next", timestamp=FIXED_TIMESTAMP),
        ]

        request = _embed_notes_into_request(messages)

        assert all("Partial thinking" not in m.get("content", "") for m in request)
