"""Tests for messages parsing."""

from core.chat.wire_shaping import _assistant_message_from_response

from .messages_test_support import (
    FIXED_TIMING,
    ChatMessage,
    ChatMessageValidationError,
    FileBlock,
    TextBlock,
    pytest,
)


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
            "work_id": "sub-work-one",
            "status": "completed",
            "timing": FIXED_TIMING,
            "change_stats": {"files": 1, "added": 2, "removed": 0, "paths": ["a.txt"]},
        }

        message = ChatMessage.from_dict(data)

        assert message.to_dict() == data

    def test_from_dict_rejects_invalid_change_stats(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(
                {
                    "id": "summary-one",
                    "timestamp": "2026-05-03T14:30:05+00:00",
                    "role": "run_summary",
                    "run_id": "run-one",
                    "status": "completed",
                    "timing": FIXED_TIMING,
                    "change_stats": {"files": -1, "added": 1, "removed": 0, "paths": []},
                }
            )

    @pytest.mark.parametrize("iteration_count", [-1, True, 1.5, "1", None])
    def test_from_dict_rejects_invalid_run_summary_iteration_count(
        self, iteration_count: object
    ) -> None:
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(
                {
                    "id": "summary-one",
                    "timestamp": "2026-05-03T14:30:05+00:00",
                    "role": "run_summary",
                    "run_id": "run-one",
                    "status": "completed",
                    "iteration_count": iteration_count,
                    "timing": FIXED_TIMING,
                }
            )

    def test_from_dict_rejects_bad_timing_duration(self):
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(
                {
                    "id": "d4e5f6",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "developer",
                    "content": "Hello",
                }
            )

    def test_user_message_rejects_model(self):
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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

        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(data)

    def test_tool_message_requires_tool_call_id(self):
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(
                {
                    "id": "error_missing_content",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "role": "error",
                    "error_kind": "provider_error",
                }
            )

    def test_from_dict_rejects_error_without_error_kind(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(
                {
                    "id": "error_missing_kind",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "role": "error",
                    "content": "Provider failed.",
                }
            )

    def test_from_dict_rejects_error_with_empty_error_kind(self):
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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
                {
                    "content": "Summary.",
                    "projection": [],
                    "compaction_policy": "custom",
                    "compaction_strategy": "custom",
                },
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

        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(data)

    def test_from_dict_usage_as_array_is_rejected(self):
        with pytest.raises(ChatMessageValidationError):
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


# ---------------------------------------------------------------------------
# Assistant ingestion hygiene: inline <think> extraction and surrogate removal.
# Models behind Ollama may embed reasoning inline in content and emit lone
# surrogates that would crash ensure_ascii=False persistence.
# ---------------------------------------------------------------------------
class TestInlineThinkingExtraction:
    def test_leading_think_block_moves_to_reasoning(self):
        message = _assistant_message_from_response(
            "ollama-cloud/qwen",
            {"content": "<think>weigh options</think>The answer is 4."},
            reasoning_scope="ollama-cloud/qwen::api-key",
        )

        assert message.content == "The answer is 4."
        assert message.reasoning == "weigh options"

    def test_leading_block_appends_to_existing_reasoning(self):
        message = _assistant_message_from_response(
            "ollama-cloud/qwen",
            {"content": "<think>inline</think>Answer", "reasoning": "field reasoning"},
        )

        assert message.content == "Answer"
        assert message.reasoning == "field reasoning\ninline"

    def test_unclosed_leading_block_is_all_thinking(self):
        message = _assistant_message_from_response(
            "ollama-cloud/qwen",
            {"content": "<thinking>partial trace"},
        )

        assert message.content is None
        assert message.reasoning == "partial trace"

    def test_thinking_only_response_has_no_content(self):
        message = _assistant_message_from_response(
            "ollama-cloud/qwen",
            {"content": "<think>only thoughts</think>"},
        )

        assert message.content is None
        assert message.reasoning == "only thoughts"

    def test_tag_inside_answer_stays_in_content(self):
        content = "Wrap your answer in <think>tags</think> like this."
        message = _assistant_message_from_response("ollama-cloud/qwen", {"content": content})

        assert message.content == content
        assert message.reasoning is None

    def test_empty_block_changes_nothing(self):
        content = "<think></think>Answer"
        message = _assistant_message_from_response("ollama-cloud/qwen", {"content": content})

        assert message.content == content
        assert message.reasoning is None

    def test_leading_reasoning_history_is_discarded_not_promoted(self):
        message = _assistant_message_from_response(
            "opencode-go/glm-5.3",
            {
                "content": (
                    "<reasoning_history>\nLet me look at the adapter changes.\n"
                    "</reasoning_history>\n"
                    "Lass mich die Provider-Adapter-Änderungen anschauen."
                ),
                "reasoning": "Let me look at the adapter changes.",
            },
        )

        assert message.content == "Lass mich die Provider-Adapter-Änderungen anschauen."
        assert message.reasoning == "Let me look at the adapter changes."

    def test_reasoning_history_before_think_is_stripped(self):
        message = _assistant_message_from_response(
            "opencode-go/glm-5.3",
            {
                "content": (
                    "<reasoning_history>\nechoed history\n</reasoning_history>\n"
                    "<think>real trace</think>Answer"
                ),
            },
        )

        assert message.content == "Answer"
        assert message.reasoning == "real trace"

    def test_think_before_reasoning_history_is_stripped(self):
        message = _assistant_message_from_response(
            "opencode-go/glm-5.3",
            {
                "content": (
                    "<think>real trace</think>"
                    "<reasoning_history>\nechoed history\n</reasoning_history>\n"
                    "Answer"
                ),
            },
        )

        assert message.content == "Answer"
        assert message.reasoning == "real trace"

    def test_reasoning_history_inside_answer_stays_in_content(self):
        content = "Do not wrap answers in <reasoning_history>tags</reasoning_history>."
        message = _assistant_message_from_response("opencode-go/glm-5.3", {"content": content})

        assert message.content == content
        assert message.reasoning is None


class TestSurrogateSanitization:
    def test_lone_surrogate_in_content_is_replaced(self):
        message = _assistant_message_from_response(
            "ollama-cloud/kimi-k2.6",
            {"content": "bad \ud800 pair"},
        )

        assert message.content == "bad \ufffd pair"

    def test_lone_surrogate_in_reasoning_is_replaced(self):
        message = _assistant_message_from_response(
            "ollama-cloud/kimi-k2.6",
            {"reasoning": "trace \udfff end", "content": "ok"},
        )

        assert message.reasoning == "trace \ufffd end"
        assert message.content == "ok"

    def test_clean_text_passes_through_unchanged(self):
        message = _assistant_message_from_response(
            "ollama-cloud/kimi-k2.6",
            {"content": "héllo wörld 🎉", "reasoning": "cléan"},
        )

        assert message.content == "héllo wörld 🎉"
        assert message.reasoning == "cléan"
