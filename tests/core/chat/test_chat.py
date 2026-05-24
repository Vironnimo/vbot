"""Phase 2 chat validation tests."""

import pytest

from core.chat import ChatMessage, ChatMessageValidationError
from core.chat.chat import _validate_assistant_message


def test_validate_assistant_message_rejects_missing_content_and_tool_calls() -> None:
    message = ChatMessage.assistant(
        model="openai/gpt-5.2",
        content=None,
        reasoning="thinking only",
    )

    with pytest.raises(ChatMessageValidationError, match="content or tool_calls"):
        _validate_assistant_message(message)
