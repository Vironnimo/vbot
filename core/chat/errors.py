"""Chat-domain exception types."""

from __future__ import annotations

from core.utils.errors import VBotError


class ChatError(VBotError):
    """Base error for chat domain failures."""


class ChatMessageValidationError(ChatError):
    """Raised when a canonical chat message is invalid."""


class ImageBudgetExceededError(ChatError):
    """New images alone exceed the Model's image-count limit; nothing was sent."""

    def __init__(self, count: int, max_count: int) -> None:
        self.count = count
        self.max_count = max_count
        super().__init__(
            f"The request contains {count} new images; this Model accepts at most "
            f"{max_count} images per request. Send or read fewer images together. "
            "No new images were silently discarded."
        )


class ChatSessionError(ChatError):
    """Raised when a chat session operation cannot be completed."""


class CompactionUnavailableError(ChatError):
    """Raised when manual Compaction has no configured execution service."""


class ToolIterationLimitError(ChatError):
    """Raised when a chat run exceeds its configured tool-iteration limit."""
