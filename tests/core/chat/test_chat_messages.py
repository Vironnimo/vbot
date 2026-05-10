"""Tests for canonical chat message primitives."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from core.chat import ChatMessage, ChatMessageValidationError, ToolCall
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

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)


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

    def test_naive_timestamp_is_rejected(self):
        with pytest.raises(ChatMessageValidationError, match="timezone"):
            ChatMessage.user("hello", timestamp=datetime(2026, 5, 3, 14, 30))


class TestChatMessageParsing:
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
