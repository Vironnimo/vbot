"""Chat history primitives: checkpoint ordinals, checkpoint guidance, compaction overlays,
reply-surface state and Session image references."""

from __future__ import annotations

import pytest

from core.chat import ChatMessage, ReplySurface, ToolCall
from core.chat._message_history import (
    checkpoint_ordinal,
    effective_compaction_messages,
    finalize_checkpoint_guidance,
    reply_surface_from_note,
    should_append_reply_surface_note,
)
from core.chat._request_history import _assign_session_image_references
from core.chat.content_blocks import ContentBlock, MediaBlock, TextBlock
from core.chat.messages import COMPACTION_CHECKPOINT_GUIDANCE, COMPACTION_SUMMARY_END_MARKER


def _checkpoint(
    summary: str = "Compacted", projection: list[ChatMessage] | None = None
) -> ChatMessage:
    return ChatMessage.compaction_checkpoint(
        summary=summary, projection=projection or [], compacted_token_count=1
    )


def _tool_batch(content: str | None = None) -> tuple[ChatMessage, ChatMessage]:
    carrier = ChatMessage.assistant(
        model="openai/gpt",
        content=content,
        tool_calls=[ToolCall(id="call-read", name="read", arguments={"path": "a"})],
    )
    result = ChatMessage.tool(tool_call_id="call-read", name="read", content='{"ok":true}')
    return carrier, result


def test_ordinals_derive_from_append_order() -> None:
    first = _checkpoint("First")
    second = _checkpoint("Second")
    without_checkpoint = [ChatMessage.user("before")]
    messages = [*without_checkpoint, first, ChatMessage.user("between"), second]

    assert effective_compaction_messages(without_checkpoint) == without_checkpoint
    assert checkpoint_ordinal(messages, first.id) == 1
    assert checkpoint_ordinal(messages, second.id) == 2
    assert checkpoint_ordinal(messages, "missing") is None


@pytest.mark.parametrize(
    "summary",
    ["Earlier decisions.", f"Earlier decisions.\n{COMPACTION_SUMMARY_END_MARKER}"],
    ids=["plain", "with-end-marker"],
)
def test_checkpoint_guidance_is_added_once_before_the_summary_end(summary: str) -> None:
    checkpoint = _checkpoint(summary)

    finalized = finalize_checkpoint_guidance(checkpoint, ordinal=3)
    finalized_again = finalize_checkpoint_guidance(finalized, ordinal=3)

    assert finalized.projection is not None
    leading = ChatMessage.from_dict(finalized.projection[0]).content
    assert isinstance(leading, str)
    guidance = COMPACTION_CHECKPOINT_GUIDANCE.format(ordinal=3)
    assert leading.startswith("[compaction-summary] Earlier decisions.")
    assert leading.count(guidance) == 1
    if COMPACTION_SUMMARY_END_MARKER in summary:
        assert leading.endswith(COMPACTION_SUMMARY_END_MARKER)
        assert leading.index(guidance) < leading.index(COMPACTION_SUMMARY_END_MARKER)
    assert finalized_again.to_dict() == finalized.to_dict()
    assert checkpoint.projection != finalized.projection


def test_compaction_overlays_a_complete_unconsumed_tool_batch_after_the_projection() -> None:
    carrier, result = _tool_batch()
    deferred_note = ChatMessage.note("after the tool batch")

    effective = effective_compaction_messages(
        [carrier, result, deferred_note, _checkpoint(projection=[deferred_note])]
    )

    assert [message.id for message in effective[-2:]] == [carrier.id, result.id]
    assert len({message.id for message in effective}) == len(effective)


@pytest.mark.parametrize("consumed", [True, False], ids=["consumed", "incomplete"])
def test_compaction_overlay_skips_a_consumed_or_incomplete_tool_batch(consumed: bool) -> None:
    carrier, result = _tool_batch()
    tail = [result, ChatMessage.assistant(model="openai/gpt", content="Done")] if consumed else []

    effective = effective_compaction_messages([carrier, *tail, _checkpoint()])

    assert {carrier.id, result.id}.isdisjoint(message.id for message in effective)


def test_compaction_overlay_precedes_messages_appended_after_the_checkpoint() -> None:
    carrier, result = _tool_batch("Checking")
    steered = ChatMessage.user("Steer")

    effective = effective_compaction_messages(
        [ChatMessage.user("Original"), carrier, result, _checkpoint(), steered]
    )

    assert [message.role for message in effective] == ["note", "assistant", "tool", "user"]
    assert [message.id for message in effective[1:]] == [carrier.id, result.id, steered.id]


def test_reply_surface_note_is_appended_after_a_switch_or_compaction() -> None:
    webui = ReplySurface.webui()
    telegram = ReplySurface.channel(
        platform="telegram", platform_display_name="Telegram", channel_id="tg-main"
    )
    webui_note = ChatMessage.note(webui.to_note_content())
    checkpoint = _checkpoint("Earlier work.")

    assert should_append_reply_surface_note([], webui) is True
    assert should_append_reply_surface_note([webui_note], webui) is False
    assert should_append_reply_surface_note([webui_note], telegram) is True
    assert should_append_reply_surface_note([webui_note, checkpoint], webui) is True
    restated = [webui_note, checkpoint, ChatMessage.note(webui.to_note_content())]
    assert should_append_reply_surface_note(restated, webui) is False


def test_old_untagged_channel_note_is_not_reply_surface_state() -> None:
    old_note = ChatMessage.note(
        "This session is receiving messages via Telegram (channel: tg-main, chat: 123)."
    )

    assert reply_surface_from_note(old_note) is None
    assert should_append_reply_surface_note([old_note], ReplySurface.webui()) is True


def _image(attachment_id: str, reference: int | None = None) -> MediaBlock:
    return MediaBlock(
        type="media",
        attachment_id=attachment_id,
        filename="image.png",
        media_type="image/png",
        image_reference=reference,
    )


def test_new_images_continue_the_sessions_image_references() -> None:
    earlier = ChatMessage.user([_image("image-one", 1)])
    content: list[ContentBlock] = [
        TextBlock(type="text", text="Compare these."),
        _image("image-two"),
        _image("image-three"),
    ]

    assert _assign_session_image_references(content, [earlier]) == [
        TextBlock(type="text", text="Compare these."),
        _image("image-two", 2),
        _image("image-three", 3),
    ]
