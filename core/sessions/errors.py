"""Failure types for the canonical SQLite Session store."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.chat.errors import ChatSessionError


class SessionStorageError(ChatSessionError):
    """Base error for an unsafe or unavailable Session storage state."""


class SessionStorageFormatError(SessionStorageError):
    """Raised when the data directory does not authorize a current-format Session store.

    Covers a missing current-format marker on an existing data-directory root,
    a malformed or newer marker, and every other state where creating or
    opening the Session database would be unauthorized.
    """


class SessionStoreSchemaMismatchError(SessionStorageFormatError):
    """Raised when a database schema is older or newer than this Runtime supports."""


class SessionRecoveryConflictError(SessionStorageError):
    """Raised when an acknowledgement refers to an incident superseded on disk."""


class SessionStoreCorruptError(SessionStorageError):
    """Raised when the canonical Session database cannot be trusted."""


class SessionStoreUnavailableError(SessionStorageError):
    """Raised when the canonical Session database cannot complete an operation."""


@dataclass(frozen=True)
class FtsHealth:
    """Verified state of the integrated derived FTS projection."""

    state: Literal["healthy", "rebuilding", "degraded", "unavailable"]
    reason: str | None = None
    generation: str | None = None
    target_high_water: int | None = None
    completed_high_water: int | None = None

    @property
    def available(self) -> bool:
        return self.state == "healthy"


@dataclass(frozen=True)
class QuarantineResult:
    """Outcome of moving a canonical database bundle to quarantine."""

    status: Literal["no_bundle", "success", "failed"]
    path: Path | None = None
    reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "success"

    @property
    def had_bundle(self) -> bool:
        return self.status != "no_bundle"
