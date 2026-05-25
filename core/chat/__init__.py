"""Chat domain public API."""

from core.chat.chat import (
    MAX_TOOL_ITERATIONS,
    ChatLoop,
    ChatMessage,
    ToolCall,
)
from core.chat.commands import (
    CommandDispatcher,
    CommandHandled,
    DispatchResult,
    NotACommand,
)
from core.chat.errors import (
    ChatError,
    ChatMessageValidationError,
    ChatSessionError,
    ToolIterationLimitError,
)
from core.sessions import ChatSession, ChatSessionManager

__all__ = [
    "ChatError",
    "ChatLoop",
    "ChatMessage",
    "ChatMessageValidationError",
    "ChatSession",
    "ChatSessionError",
    "ChatSessionManager",
    "CommandDispatcher",
    "CommandHandled",
    "DispatchResult",
    "MAX_TOOL_ITERATIONS",
    "NotACommand",
    "ToolCall",
    "ToolIterationLimitError",
]
