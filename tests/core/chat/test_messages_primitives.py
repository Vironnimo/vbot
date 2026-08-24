"""Tool call, sender, reply-surface, factory, and parsing tests."""

from core.chat.messages import ToolCallRejection, _assistant_message_from_response
from core.chat.output_files import AssistantFileReference

from .messages_test_support import (
    ERROR_KIND_RATE_LIMIT,
    FIXED_TIMESTAMP,
    FIXED_TIMING,
    ChatError,
    ChatMessage,
    ChatMessageValidationError,
    FileBlock,
    FrozenInstanceError,
    MessageSender,
    ReplySurface,
    TextBlock,
    ToolCall,
    _assistant_continuation_dict,
    _embed_notes_into_request,
    _message_to_request_dict,
    datetime,
    pytest,
    reply_surface_from_note,
    should_append_reply_surface_note,
)


class TestToolCall:
    def test_to_dict_returns_canonical_fields(self):
        tool_call = ToolCall(id="call_abc", name="get_weather", arguments={"city": "Berlin"})

        assert tool_call.to_dict() == {
            "id": "call_abc",
            "name": "get_weather",
            "arguments": {"city": "Berlin"},
        }

    def test_from_dict_rejects_non_object_arguments(self):
        with pytest.raises(ChatMessageValidationError):
            ToolCall.from_dict({"id": "call_abc", "name": "get_weather", "arguments": []})

    def test_rejection_round_trips_with_canonical_tool_call(self):
        rejection = ToolCallRejection(
            code="malformed_tool_arguments",
            message="Arguments were malformed.",
            fingerprint="sha256",
        )
        tool_call = ToolCall(
            id="call_bad",
            name="write",
            arguments={},
            rejection=rejection,
        )

        assert ToolCall.from_dict(tool_call.to_dict()) == tool_call

    def test_argument_sequence_metadata_round_trips_with_canonical_tool_call(self):
        tool_call = ToolCall(
            id="call_batch",
            name="bash",
            arguments={"command": "echo one"},
            argument_sequence_index=0,
            argument_sequence_length=2,
        )

        assert ToolCall.from_dict(tool_call.to_dict()) == tool_call

    def test_argument_sequence_metadata_requires_a_complete_valid_pair(self):
        with pytest.raises(ChatMessageValidationError):
            ToolCall(
                id="call_batch",
                name="bash",
                argument_sequence_index=0,
            )

    def test_frozen(self):
        tool_call = ToolCall(id="call_abc", name="get_weather")

        with pytest.raises(FrozenInstanceError):
            tool_call.name = "changed"  # type: ignore[misc]


class TestMessageSender:
    def test_to_dict_returns_canonical_fields(self):
        sender = MessageSender(id="50", display_name="Alice")

        assert sender.to_dict() == {
            "id": "50",
            "display_name": "Alice",
            "role": "member",
        }

    def test_from_dict_round_trips(self):
        sender = MessageSender(id="50", display_name="Alice", role="admin")

        assert MessageSender.from_dict(sender.to_dict()) == sender

    def test_from_dict_defaults_legacy_sender_to_member(self):
        assert MessageSender.from_dict({"id": "50", "display_name": "Alice"}) == MessageSender(
            id="50", display_name="Alice", role="member"
        )

    def test_from_dict_rejects_unknown_role(self):
        with pytest.raises(ChatMessageValidationError):
            MessageSender.from_dict({"id": "50", "display_name": "Alice", "role": "owner"})

    @pytest.mark.parametrize("bad_id", [None, "", 50, {"nested": True}])
    def test_from_dict_rejects_bad_id(self, bad_id):
        with pytest.raises(ChatMessageValidationError):
            MessageSender.from_dict({"id": bad_id, "display_name": "Alice"})

    @pytest.mark.parametrize("bad_display_name", [None, "", 50, ["Alice"]])
    def test_from_dict_rejects_bad_display_name(self, bad_display_name):
        with pytest.raises(
            ChatMessageValidationError,
        ):
            MessageSender.from_dict({"id": "50", "display_name": bad_display_name})

    def test_frozen(self):
        sender = MessageSender(id="50", display_name="Alice")

        with pytest.raises(FrozenInstanceError):
            sender.display_name = "changed"  # type: ignore[misc]


class TestReplySurface:
    def test_invalid_cross_kind_fields_are_rejected(self):
        with pytest.raises(ChatError):
            ReplySurface(kind="webui", channel_id="tg-main")

    def test_webui_note_round_trips_and_renders_exact_reminder(self):
        surface = ReplySurface.webui()
        note = ChatMessage.note(surface.to_note_content())

        assert reply_surface_from_note(note) == surface
        assert _embed_notes_into_request([note]) == [
            {
                "role": "user",
                "content": (
                    "<system-reminder>\n"
                    "To show the user an image or provide a file download, include "
                    "file:<filesystem-path> in your reply; vBot renders it automatically.\n"
                    "</system-reminder>"
                ),
            }
        ]

    def test_assistant_output_files_round_trip_but_never_reach_provider_requests(self):
        reference = AssistantFileReference(
            line_index=0,
            path="C:\\work\\chart.png",
            start_index=7,
            end_index=29,
        )
        message = ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Chart: file:C:\\work\\chart.png",
            output_files=[reference],
        )

        assert ChatMessage.from_dict(message.to_dict()) == message
        assert "output_files" not in _message_to_request_dict(message)
        assert "output_files" not in _assistant_continuation_dict(message)

    def test_channel_note_round_trips_and_renders_exact_reminder(self):
        surface = ReplySurface.channel(
            platform="telegram",
            platform_display_name="Telegram",
            channel_id="tg-main",
        )
        note = ChatMessage.note(surface.to_note_content())

        assert reply_surface_from_note(note) == surface
        content = _embed_notes_into_request([note])[0]["content"]
        assert content.startswith("<system-reminder>\n")
        assert content.endswith("\n</system-reminder>")
        assert "Telegram" in content
        assert "tg-main" in content

    @pytest.mark.parametrize(
        ("platform", "display_name"),
        [("telegram", "Telegram"), ("discord", "Discord")],
    )
    def test_group_channel_note_states_conversation_kind(self, platform: str, display_name: str):
        direct = ReplySurface.channel(
            platform=platform,
            platform_display_name=display_name,
            channel_id=f"{platform}-main",
        )
        group = ReplySurface.channel(
            platform=platform,
            platform_display_name=display_name,
            channel_id=f"{platform}-main",
            conversation_kind="group",
        )
        note = ChatMessage.note(group.to_note_content())

        assert reply_surface_from_note(note) == group
        assert direct.identity != group.identity
        assert _embed_notes_into_request([note])[0]["content"].startswith(
            f"<system-reminder>\nThe current conversation is a group chat on {display_name}. "
        )

    def test_append_decision_uses_latest_tag_switch_and_compaction_chronology(self):
        webui = ReplySurface.webui()
        telegram = ReplySurface.channel(
            platform="telegram",
            platform_display_name="Telegram",
            channel_id="tg-main",
        )
        webui_note = ChatMessage.note(webui.to_note_content())

        assert should_append_reply_surface_note([], webui) is True
        assert should_append_reply_surface_note([webui_note], webui) is False
        assert should_append_reply_surface_note([webui_note], telegram) is True

        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Earlier work.",
            projection=[],
            compacted_token_count=10,
        )
        assert should_append_reply_surface_note([webui_note, checkpoint], webui) is True
        assert (
            should_append_reply_surface_note(
                [webui_note, checkpoint, ChatMessage.note(webui.to_note_content())], webui
            )
            is False
        )

    def test_old_untagged_channel_note_is_not_reply_surface_state(self):
        old_note = ChatMessage.note(
            "This session is receiving messages via Telegram (channel: tg-main, chat: 123)."
        )

        assert reply_surface_from_note(old_note) is None
        assert should_append_reply_surface_note([old_note], ReplySurface.webui()) is True


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
            "sender": {"id": "50", "display_name": "Alice", "role": "member"},
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

    def test_assistant_message_round_trips_provider_phase(self):
        message = ChatMessage.assistant(
            model="openai/gpt-5.5",
            content="I will inspect this.",
            reasoning="Working.",
            reasoning_scope="openai/gpt-5.5::api-key",
            phase="commentary",
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.to_dict()["phase"] == "commentary"
        restored = ChatMessage.from_dict(message.to_dict())
        assert restored.phase == "commentary"
        assert restored.reasoning_scope == "openai/gpt-5.5::api-key"

    def test_non_assistant_message_rejects_phase(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(
                {
                    "id": "u1",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "user",
                    "content": "hi",
                    "phase": "commentary",
                }
            )

    def test_assistant_message_rejects_non_string_phase(self):
        with pytest.raises(
            ChatMessageValidationError,
        ):
            ChatMessage.assistant(
                model="openai/gpt-5.5",
                content="Checking.",
                phase=1,  # type: ignore[arg-type]
                timestamp=FIXED_TIMESTAMP,
            ).to_dict()

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

    def test_tool_message_persists_display_but_provider_projection_strips_it(self):
        display = {
            "version": 1,
            "primary": [{"kind": "path", "value": "src/app.py"}],
            "facts": [],
        }
        message = ChatMessage.tool(
            tool_call_id="call_abc",
            name="read",
            content='{"ok":true}',
            tool_display=display,
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.to_dict()["tool_display"] == display
        assert ChatMessage.from_dict(message.to_dict()).tool_display == display
        assert "tool_display" not in _message_to_request_dict(message)

    def test_non_tool_message_rejects_tool_display(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage(
                id="user-1",
                timestamp="2026-05-03T14:30:00+00:00",
                role="user",
                content="hello",
                tool_display={"version": 1},
            ).to_dict()

    def test_run_summary_contains_run_status_and_timing(self):
        message = ChatMessage.run_summary(
            run_id="run-one",
            work_id="sub-work-one",
            status="completed",
            timing=FIXED_TIMING,
            iteration_count=3,
            change_stats={"files": 2, "added": 5, "removed": 1, "paths": ["a.txt"]},
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.to_dict() == {
            "id": message.id,
            "timestamp": "2026-05-03T14:30:00+00:00",
            "role": "run_summary",
            "timing": FIXED_TIMING,
            "run_id": "run-one",
            "work_id": "sub-work-one",
            "status": "completed",
            "iteration_count": 3,
            "change_stats": {"files": 2, "added": 5, "removed": 1, "paths": ["a.txt"]},
        }

    def test_assistant_message_with_usage(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="The answer is 42.",
            usage={
                "input_tokens": 150,
                "output_tokens": 12,
                "cache_write_tokens": 20,
                "reasoning_tokens": 7,
            },
            timestamp=FIXED_TIMESTAMP,
        )

        expected_usage = {
            "input_tokens": 150,
            "output_tokens": 12,
            "cache_write_tokens": 20,
            "reasoning_tokens": 7,
        }
        assert message.usage == expected_usage
        result = message.to_dict()
        assert result["usage"] == expected_usage

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
            interruption_cause="timeout",
            timestamp=FIXED_TIMESTAMP,
        )

        result = message.to_dict()
        assert result["interrupted"] is True
        assert result["interruption_cause"] == "timeout"
        restored = ChatMessage.from_dict(result)
        assert restored.interrupted is True
        assert restored.interruption_cause == "timeout"

    def test_assistant_message_not_interrupted_omits_flag(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="Complete answer",
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.interrupted is False
        assert message.interruption_cause is None
        assert "interrupted" not in message.to_dict()
        assert "interruption_cause" not in message.to_dict()

    def test_interrupted_rejected_on_non_assistant_role(self):
        with pytest.raises(ChatMessageValidationError):
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
        with pytest.raises(ChatMessageValidationError):
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

    def test_interruption_cause_requires_interrupted_assistant(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(
                {
                    "id": "a1",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "assistant",
                    "model": "openai/gpt-4.1",
                    "content": "hi",
                    "interruption_cause": "timeout",
                }
            )

    def test_interruption_cause_rejects_unknown_value(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.from_dict(
                {
                    "id": "a1",
                    "timestamp": "2026-05-03T14:30:01+00:00",
                    "role": "assistant",
                    "model": "openai/gpt-4.1",
                    "content": "hi",
                    "interrupted": True,
                    "interruption_cause": "mystery",
                }
            )


class TestReasoningTiming:
    def test_assistant_message_round_trips_reasoning_timing(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="Answer",
            reasoning="Thought",
            reasoning_timing=FIXED_TIMING,
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.reasoning_timing == FIXED_TIMING
        assert ChatMessage.from_dict(message.to_dict()).reasoning_timing == FIXED_TIMING

    def test_assistant_message_without_reasoning_omits_reasoning_timing(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="Answer",
            timestamp=FIXED_TIMESTAMP,
        )

        assert message.to_dict().get("reasoning_timing") is None

    def test_reasoning_timing_requires_reasoning(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.assistant(
                model="openai/gpt-4.1",
                content="Answer",
                reasoning_timing=FIXED_TIMING,
                timestamp=FIXED_TIMESTAMP,
            ).to_dict()

    def test_reasoning_timing_rejects_bad_payload(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage.assistant(
                model="openai/gpt-4.1",
                content="Answer",
                reasoning="Thought",
                reasoning_timing={"started_at": "no-offset", "completed_at": "x", "duration_ms": 1},
                timestamp=FIXED_TIMESTAMP,
            ).to_dict()

    def test_user_message_rejects_reasoning_timing(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage(
                id="user-1",
                timestamp="2026-05-03T14:30:00+00:00",
                role="user",
                content="hello",
                reasoning_timing=FIXED_TIMING,
            ).to_dict()

    def test_tool_message_rejects_reasoning_timing(self):
        with pytest.raises(ChatMessageValidationError):
            ChatMessage(
                id="tool-1",
                timestamp="2026-05-03T14:30:00+00:00",
                role="tool",
                content="{}",
                tool_call_id="call_abc",
                name="read",
                reasoning_timing=FIXED_TIMING,
            ).to_dict()

    def test_provider_request_projection_strips_reasoning_timing(self):
        message = ChatMessage.assistant(
            model="openai/gpt-4.1",
            content="Answer",
            reasoning="Thought",
            reasoning_timing=FIXED_TIMING,
            timestamp=FIXED_TIMESTAMP,
        )

        assert "reasoning_timing" not in _message_to_request_dict(message)
        assert "reasoning_timing" not in _assistant_continuation_dict(message)

    def test_response_construction_keeps_timing_only_with_reasoning(self):
        timing = dict(FIXED_TIMING)
        message = _assistant_message_from_response(
            "openai/gpt-4.1",
            {"content": "Answer", "reasoning": "Thought", "reasoning_timing": timing},
            reasoning_timing=timing,
        )
        bare = _assistant_message_from_response(
            "openai/gpt-4.1",
            {"content": "Answer", "reasoning_timing": timing},
            reasoning_timing=timing,
        )

        assert message.reasoning_timing == timing
        # No reasoning text means no measurable block; the span is dropped.
        assert bare.reasoning_timing is None

    def test_naive_timestamp_is_rejected(self):
        with pytest.raises(ChatMessageValidationError):
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
