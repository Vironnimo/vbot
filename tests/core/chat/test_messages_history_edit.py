"""Append-only Session lineage editing tests."""

from datetime import UTC, datetime

import pytest

from core.chat import ChatMessage, ChatMessageValidationError
from core.sessions import ChatSessionError, editable_session_message_index

FIXED_TIMESTAMP = datetime(2026, 5, 3, 14, 30, tzinfo=UTC)


def test_history_edit_round_trips_without_user_content() -> None:
    marker = ChatMessage.history_edit("user-one", timestamp=FIXED_TIMESTAMP)

    assert marker.to_dict() == {
        "id": marker.id,
        "timestamp": "2026-05-03T14:30:00.000000Z",
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


def test_edit_targets_only_an_own_plain_text_user_message_after_the_latest_takeover() -> None:
    first = ChatMessage.user("first", timestamp=FIXED_TIMESTAMP)
    replacement = ChatMessage.user("replacement", timestamp=FIXED_TIMESTAMP)

    assert editable_session_message_index([first, replacement], replacement.id) == 1
    with pytest.raises(ChatSessionError, match="not active"):
        editable_session_message_index([replacement], first.id)

    structured = ChatMessage.user([], timestamp=FIXED_TIMESTAMP)
    with pytest.raises(ChatSessionError, match="plain-text"):
        editable_session_message_index([structured], structured.id)

    takeover = ChatMessage.agent_takeover(
        from_address="alpha",
        to_address="beta",
        timestamp=FIXED_TIMESTAMP,
    )
    with pytest.raises(ChatSessionError, match="takeover"):
        editable_session_message_index([first, takeover], first.id)
