"""Chat message history and compaction primitive tests."""

import pytest

from .messages_test_support import (
    ChatMessage,
    ChatMessageValidationError,
    ToolCall,
    _effective_compaction_messages,
    checkpoint_ordinal,
    finalize_checkpoint_history_guidance,
    history_available,
)


class TestHistoryCompactionPrimitives:
    def test_availability_and_ordinals_derive_from_append_order(self) -> None:
        first = ChatMessage.compaction_checkpoint(
            summary="First",
            projection=[],
            compacted_token_count=1,
        )
        second = ChatMessage.compaction_checkpoint(
            summary="Second",
            projection=[],
            compacted_token_count=2,
        )
        without_checkpoint = [ChatMessage.user("before")]
        messages = [*without_checkpoint, first, ChatMessage.user("between"), second]

        assert history_available(without_checkpoint) is False
        assert history_available(messages) is True
        assert checkpoint_ordinal(messages, first.id) == 1
        assert checkpoint_ordinal(messages, second.id) == 2
        assert checkpoint_ordinal(messages, "missing") is None

    def test_checkpoint_guidance_is_added_once(self) -> None:
        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Earlier decisions.",
            projection=[],
            compacted_token_count=1,
        )

        finalized = finalize_checkpoint_history_guidance(checkpoint, ordinal=3)
        finalized_again = finalize_checkpoint_history_guidance(finalized, ordinal=3)

        assert finalized.projection is not None
        leading = ChatMessage.from_dict(finalized.projection[0])
        assert isinstance(leading.content, str)
        assert leading.content.startswith("[compaction-summary] Earlier decisions.")
        assert finalized_again.to_dict() == finalized.to_dict()
        assert checkpoint.projection != finalized.projection

    def test_chat_stamps_final_context_projection_onto_checkpoint(self) -> None:
        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Earlier decisions.",
            projection=[],
            compacted_token_count=123,
        )

        stamped = checkpoint.with_compaction_context_tokens(
            context_tokens_before=155_499,
            context_tokens_after=34_691,
        )

        assert stamped.usage == {
            "compacted_token_count": 123,
            "context_tokens_before": 155_499,
            "context_tokens_after": 34_691,
        }
        assert checkpoint.usage == {"compacted_token_count": 123}

    def test_duration_stamp_adds_observed_wall_clock_time(self) -> None:
        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Earlier decisions.",
            projection=[],
            compacted_token_count=123,
        )

        stamped = checkpoint.with_compaction_duration_ms(duration_ms=54_000)

        assert stamped.usage == {
            "compacted_token_count": 123,
            "compaction_duration_ms": 54_000,
        }
        assert checkpoint.usage == {"compacted_token_count": 123}

    def test_duration_stamp_rejects_non_integer_and_negative_values(self) -> None:
        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Earlier decisions.",
            projection=[],
            compacted_token_count=123,
        )

        with pytest.raises(ChatMessageValidationError):
            checkpoint.with_compaction_duration_ms(duration_ms=-1)
        with pytest.raises(ChatMessageValidationError):
            checkpoint.with_compaction_duration_ms(duration_ms="54000")  # type: ignore[arg-type]
        with pytest.raises(ChatMessageValidationError):
            checkpoint.with_compaction_duration_ms(duration_ms=True)  # type: ignore[arg-type]

    def test_textual_checkpoint_ends_provider_reasoning_state_but_keeps_phase(self) -> None:
        assistant = ChatMessage.assistant(
            model="openai/gpt-5.6-sol",
            content="Prior answer",
            reasoning="Readable summary",
            reasoning_meta={
                "response_output": [
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
                ]
            },
            reasoning_scope="openai/gpt-5.6-sol::api-key",
            reasoning_timing={"first_delta_ms": 120, "last_delta_ms": 900},
            phase="final_answer",
        )

        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Earlier context.",
            projection=[assistant],
            compacted_token_count=10,
        )

        assert checkpoint.projection is not None
        projected_assistant = ChatMessage.from_dict(checkpoint.projection[1])
        assert projected_assistant.reasoning is None
        assert projected_assistant.reasoning_meta is None
        assert projected_assistant.reasoning_scope is None
        assert projected_assistant.reasoning_timing is None
        assert projected_assistant.phase == "final_answer"

    def test_post_compaction_context_overlays_complete_unconsumed_tool_batch(self) -> None:
        carrier = ChatMessage.assistant(
            model="openai/gpt",
            content=None,
            tool_calls=[ToolCall(id="call-read", name="read", arguments={"path": "a"})],
        )
        result = ChatMessage.tool(
            tool_call_id="call-read",
            name="read",
            content='{"ok":true}',
        )
        deferred_note = ChatMessage.note("after the tool batch")
        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Compacted",
            projection=[deferred_note],
            compacted_token_count=1,
        )

        effective = _effective_compaction_messages([carrier, result, deferred_note, checkpoint])

        assert [message.id for message in effective[-2:]] == [carrier.id, result.id]
        assert len({message.id for message in effective}) == len(effective)

    def test_pending_batch_overlay_stops_after_later_assistant(self) -> None:
        carrier = ChatMessage.assistant(
            model="openai/gpt",
            content=None,
            tool_calls=[ToolCall(id="call-read", name="read", arguments={})],
        )
        result = ChatMessage.tool(
            tool_call_id="call-read",
            name="read",
            content='{"ok":true}',
        )
        consumed = ChatMessage.assistant(model="openai/gpt", content="Done")
        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Compacted",
            projection=[],
            compacted_token_count=1,
        )

        effective = _effective_compaction_messages([carrier, result, consumed, checkpoint])

        assert carrier.id not in {message.id for message in effective}
        assert result.id not in {message.id for message in effective}
