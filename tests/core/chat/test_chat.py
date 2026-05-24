"""Phase 2 chat validation tests."""

import pytest

from core.chat import ChatMessage, ChatMessageValidationError
from core.chat.chat import _validate_assistant_message


def test_validate_assistant_message_allows_reasoning_only() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
        reasoning="thinking only",
    )

    _validate_assistant_message(message)


def test_validate_assistant_message_allows_reasoning_meta_only() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
        reasoning_meta={"provider": "opaque"},
    )

    _validate_assistant_message(message)


def test_validate_assistant_message_rejects_truly_empty_assistant() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
    )

    with pytest.raises(ChatMessageValidationError, match="content, reasoning, reasoning_meta"):
        _validate_assistant_message(message)
