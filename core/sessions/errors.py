"""Failure types for the canonical SQLite Session store."""

from __future__ import annotations

from core.chat.errors import ChatSessionError


class SessionStorageError(ChatSessionError):
    """Base error for an unsafe or unavailable Session storage state."""


class SessionStorageFormatError(SessionStorageError):
    """Raised when the data directory does not authorize a current-format Session store.

    Covers a missing current-format marker on an existing data-directory root,
    a malformed or newer marker, and every other state where creating or
    opening the Session database would be unauthorized.
    """


class SessionStoreCorruptError(SessionStorageError):
    """Raised when the canonical Session database cannot be trusted."""


class SessionStoreUnavailableError(SessionStorageError):
    """Raised when the canonical Session database cannot complete an operation."""
