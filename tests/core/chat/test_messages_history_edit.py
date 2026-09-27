"""Append-only Session lineage editing tests."""

from datetime import UTC, datetime

import pytest

from core.chat import ChatMessage, ChatMessageValidationError

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
