"""Chat message history and compaction primitive tests."""

from .messages_test_support import (
    HISTORY_COMPACTION_GUIDANCE,
    ChatMessage,
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

    def test_checkpoint_guidance_is_exact_and_added_once(self) -> None:
        checkpoint = ChatMessage.compaction_checkpoint(
            summary="Earlier decisions.",
            projection=[],
            compacted_token_count=1,
        )

        finalized = finalize_checkpoint_history_guidance(checkpoint, ordinal=3)
        finalized_again = finalize_checkpoint_history_guidance(finalized, ordinal=3)

        assert finalized.projection is not None
        leading = ChatMessage.from_dict(finalized.projection[0])
        guidance = HISTORY_COMPACTION_GUIDANCE.format(ordinal=3)
        assert leading.content == f"[compaction-summary] Earlier decisions.\n\n{guidance}"
        assert finalized_again.to_dict() == finalized.to_dict()
        assert checkpoint.projection != finalized.projection

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
