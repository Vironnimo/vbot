"""Chat-domain exception types."""

from __future__ import annotations

from core.utils.errors import VBotError


class ChatError(VBotError):
    """Base error for chat domain failures."""


class ChatMessageValidationError(ChatError):
    """Raised when a canonical chat message is invalid."""


class ImageBudgetExceededError(ChatError):
    """Fresh images alone exceed the harness request budget; nothing was sent."""

    def __init__(self, count: int, size_bytes: int, max_count: int, max_bytes: int) -> None:
        self.count = count
        self.size_bytes = size_bytes
        self.max_count = max_count
        self.max_bytes = max_bytes
        super().__init__(
            f"The request contains {count} new images using {size_bytes / 1024**2:.1f} MiB "
            f"of image data. vBot allows at most {max_count} images and "
            f"{max_bytes / 1024**2:g} MiB per request. Send or read fewer images together; "
            "use a smaller copy if one image exceeds the data limit. "
            "No new images were silently discarded."
        )


class ChatSessionError(ChatError):
    """Raised when a chat session operation cannot be completed."""


class CompactionUnavailableError(ChatError):
    """Raised when manual Compaction has no configured execution service."""


class ToolIterationLimitError(ChatError):
    """Raised when a chat run exceeds its configured tool-iteration limit."""
