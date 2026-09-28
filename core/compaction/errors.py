"""Compaction planning and execution failures."""

from core.utils.errors import VBotError


class CompactionError(VBotError):
    """Raised when a compaction plan cannot be produced or executed.

    ``model`` names the Model reference whose Compaction call failed; it stays
    ``None`` for failures before or after that call.
    """

    def __init__(self, message: str = "", *, model: str | None = None) -> None:
        super().__init__(message)
        self.model = model


class CompactionInsufficientReclaimError(CompactionError):
    """Raised when an automatic checkpoint would not reclaim enough Context."""
