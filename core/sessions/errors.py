"""Failure types of the Session store.

Storage failures are the shared database kernel's errors
(``core.database.DatabaseError`` and its kinds), never ``ChatSessionError``:
a broken or busy database must not read as a missing Session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.chat.errors import ChatSessionError
from core.database.errors import DatabaseCorruptError


class SessionNotFoundError(ChatSessionError):
    """Raised when a requested live Session generation does not exist."""


class SessionPageCursorError(ChatSessionError):
    """Raised when a bounded Session read references an invalid page anchor."""


class SessionStoreCorruptError(DatabaseCorruptError):
    """Raised when stored Session rows cannot be trusted."""


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
