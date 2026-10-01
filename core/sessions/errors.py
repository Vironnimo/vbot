"""Failure types of the Session store.

Storage failures are the shared database kernel's errors
(``core.database.DatabaseError`` and its kinds), never ``ChatSessionError``:
a broken or busy database must not read as a missing Session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.chat.errors import ChatSessionError
from core.database.errors import DatabaseCorruptError

if TYPE_CHECKING:
    from core.sessions._types import SessionAddress


class SessionNotFoundError(ChatSessionError):
    """Raised when a requested live Session generation does not exist."""


class SessionPageCursorError(ChatSessionError):
    """Raised when a bounded Session read references an invalid page anchor."""


class SessionStoreCorruptError(DatabaseCorruptError):
    """Raised when stored Session rows cannot be trusted."""


class ArchiveEntryError(Exception):
    """An archive entry operation was refused; nothing changed."""


class ArchiveEntryNotFoundError(ArchiveEntryError):
    """Raised when no archive entry has the given id (or ids)."""

    def __init__(self, entry_id: str, *more_entry_ids: str) -> None:
        entry_ids = (entry_id, *more_entry_ids)
        noun = "archive entry" if len(entry_ids) == 1 else "archive entries"
        super().__init__(f"{noun} not found: {', '.join(entry_ids)}")
        self.entry_id = entry_id
        self.entry_ids = entry_ids


class ArchiveEntryBusyError(ArchiveEntryError):
    """Raised when another operation holds the entry (archiving, restoring or purging)."""

    def __init__(self, entry_id: str, state: str) -> None:
        super().__init__(
            f"archive entry {entry_id} is {state}; retry after that operation finished"
        )
        self.entry_id = entry_id
        self.state = state


class ArchiveMembersManagedError(ArchiveEntryError):
    """Raised when an Extension manages member Sessions a restore would make live."""

    def __init__(self, entry_id: str) -> None:
        super().__init__(
            f"cannot restore archive entry {entry_id}: an Extension manages its Sessions; "
            "use that Extension to resume them"
        )
        self.entry_id = entry_id


class ArchiveAddressTakenError(ArchiveEntryError):
    """Raised when live Sessions occupy addresses a restore would give back."""

    def __init__(self, entry_id: str, addresses: tuple[SessionAddress, ...]) -> None:
        super().__init__(
            f"cannot restore archive entry {entry_id}: live Sessions already use "
            + ", ".join(address.session_id for address in addresses)
        )
        self.entry_id = entry_id
        self.addresses = addresses


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
