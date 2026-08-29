"""Append-only Session lineage editing tests."""

from datetime import UTC, datetime

import pytest

from core.chat import ChatMessage, ChatMessageValidationError
from core.chat.messages import _effective_compaction_messages
from core.sessions import (
    ChatSessionError,
    active_session_messages,
    editable_session_message_ids,
)

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)


def test_history_edit_round_trips_without_user_content() -> None:
    marker = ChatMessage.history_edit("user-one", timestamp=FIXED_TIMESTAMP)

    assert marker.to_dict() == {
        "id": marker.id,
        "timestamp": "2026-05-03T14:30:00+00:00",
        "role": "history_edit",
        "target_message_id": "user-one",
    }
    assert ChatMessage.from_dict(marker.to_dict()) == marker


def test_history_edit_requires_only_a_target_message_id() -> None:
    with pytest.raises(ChatMessageValidationError, match="target_message_id"):
        ChatMessage.from_dict(
            {
                "id": "edit-one",
                "timestamp": "2026-05-03T14:30:00+00:00",
                "role": "history_edit",
            }
        )
    with pytest.raises(ChatMessageValidationError, match="content"):
        ChatMessage.from_dict(
            {
                "id": "edit-one",
                "timestamp": "2026-05-03T14:30:00+00:00",
                "role": "history_edit",
                "target_message_id": "user-one",
                "content": "hidden mutation",
            }
        )


def test_active_lineage_preserves_raw_records_and_folds_multiple_edits() -> None:
    first = ChatMessage.user("first", timestamp=FIXED_TIMESTAMP)
    first_answer = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="old answer",
        usage={"input_tokens": 10, "output_tokens": 2},
        timestamp=FIXED_TIMESTAMP,
    )
    second = ChatMessage.user("second", timestamp=FIXED_TIMESTAMP)
    first_edit = ChatMessage.history_edit(first.id, timestamp=FIXED_TIMESTAMP)
    replacement = ChatMessage.user("replacement", timestamp=FIXED_TIMESTAMP)
    replacement_answer = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content="replacement answer",
        usage={"input_tokens": 20, "output_tokens": 3},
        timestamp=FIXED_TIMESTAMP,
    )
    second_edit = ChatMessage.history_edit(replacement.id, timestamp=FIXED_TIMESTAMP)
    final = ChatMessage.user("final", timestamp=FIXED_TIMESTAMP)
    raw = [
        first,
        first_answer,
        second,
        first_edit,
        replacement,
        replacement_answer,
        second_edit,
        final,
    ]

    active = active_session_messages(raw)

    assert [message.content for message in active] == ["final"]
    assert len(raw) == 8
    assert first_answer.usage == {"input_tokens": 10, "output_tokens": 2}
    assert replacement_answer.usage == {"input_tokens": 20, "output_tokens": 3}


def test_active_lineage_rejects_inactive_structured_and_pre_takeover_targets() -> None:
    first = ChatMessage.user("first", timestamp=FIXED_TIMESTAMP)
    marker = ChatMessage.history_edit(first.id, timestamp=FIXED_TIMESTAMP)
    replacement = ChatMessage.user("replacement", timestamp=FIXED_TIMESTAMP)

    with pytest.raises(ChatSessionError, match="not active"):
        active_session_messages([first, marker, replacement, ChatMessage.history_edit(first.id)])

    structured = ChatMessage.user([], timestamp=FIXED_TIMESTAMP)
    with pytest.raises(ChatSessionError, match="plain-text"):
        active_session_messages([structured, ChatMessage.history_edit(structured.id)])

    takeover = ChatMessage.agent_takeover(
        from_address="alpha",
        to_address="beta",
        timestamp=FIXED_TIMESTAMP,
    )
    with pytest.raises(ChatSessionError, match="takeover"):
        active_session_messages([first, takeover, ChatMessage.history_edit(first.id)])


def test_editable_ids_and_compaction_projection_use_only_active_lineage() -> None:
    first = ChatMessage.user("first", timestamp=FIXED_TIMESTAMP)
    old_checkpoint = ChatMessage.compaction_checkpoint(
        summary="old summary",
        projection=[first],
        compacted_token_count=100,
        policy="context_ratio",
        strategy="summary_tail",
        timestamp=FIXED_TIMESTAMP,
    )
    marker = ChatMessage.history_edit(first.id, timestamp=FIXED_TIMESTAMP)
    replacement = ChatMessage.user("replacement", timestamp=FIXED_TIMESTAMP)
    raw = [first, old_checkpoint, marker, replacement]

    assert editable_session_message_ids(raw) == frozenset({replacement.id})
    assert _effective_compaction_messages(raw) == [replacement]
