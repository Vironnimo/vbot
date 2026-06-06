"""Storage domain errors."""

from __future__ import annotations

from core.utils.errors import VBotError


class StorageError(VBotError):
    """Raised for invalid storage data or unsafe storage paths."""
