"""Failure types for the canonical SQLite Session store."""

from __future__ import annotations

from core.chat.errors import ChatSessionError


class SessionStorageError(ChatSessionError):
    """Base error for an unsafe or unavailable Session storage state."""


class SessionConversionRequiredError(SessionStorageError):
    """Raised when live legacy transcripts require offline conversion."""


class SessionStorageConflictError(SessionStorageError):
    """Raised when SQLite and live JSONL transcripts coexist."""


class SessionConversionIncompleteError(SessionStorageError):
    """Raised while an interrupted conversion marker is present."""


class SessionStoreCorruptError(SessionStorageError):
    """Raised when the canonical Session database cannot be trusted."""
