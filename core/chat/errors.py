"""Chat-domain exception types."""

from __future__ import annotations

from core.utils.errors import VBotError


class ChatError(VBotError):
    """Base error for chat domain failures."""


class ChatMessageValidationError(ChatError):
    """Raised when a canonical chat message is invalid."""


class ChatSessionError(ChatError):
    """Raised when a chat session operation cannot be completed."""


class ToolIterationLimitError(ChatError):
    """Raised when a chat run exceeds its configured tool-iteration limit."""
