"""Results of archive operations and reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.sessions import (
    ArchiveEntry,
    ArchiveEntryCursor,
    ArchiveMember,
    ArchiveTree,
    SessionAddress,
)

# Restore problems another target id avoids; every other blocker it does not.
RESTORE_CONFLICT_CODES = frozenset({"agent_id_taken", "project_id_taken", "session_address_taken"})


@dataclass(frozen=True)
class RestoreProblem:
    """One reason a restore is refused (blocker) or proceeds differently (warning)."""

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RestoreCheck:
    """What a restore of one entry under ``target_id`` would meet; nothing is changed."""

    entry_id: str
    kind: str
    target_id: str
    blockers: tuple[RestoreProblem, ...] = ()
    warnings: tuple[RestoreProblem, ...] = ()

    @property
    def possible(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class AgentArchiveOutcome:
    """An archived Identity Agent: its entry, how many Sessions it took, the changed grants."""

    entry_id: str
    agent_id: str
    session_count: int
    policy_agent_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProjectArchiveOutcome:
    """An archived Project and the Identity Agents it unrooted."""

    entry_id: str
    project_id: str
    session_count: int
    affected_agent_ids: tuple[str, ...]
    copied_files: Mapping[str, tuple[str, ...]]
    backed_up_files: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class SessionArchiveOutcome:
    """An archived Session and, for an Identity Agent, its current Session afterwards.

    ``was_current`` tells whether the archived Session was that current Session.
    """

    entry_id: str
    address: SessionAddress
    next_session_id: str | None
    was_current: bool


@dataclass(frozen=True)
class RestoreOutcome:
    """A restored entry: where its Agent, Project or Sessions live now."""

    entry_id: str
    kind: str
    subject_id: str
    target_id: str
    addresses: tuple[SessionAddress, ...]
    warnings: tuple[RestoreProblem, ...]


@dataclass(frozen=True)
class PurgedEntry:
    entry_id: str
    kind: str
    subject_id: str
    session_count: int


@dataclass(frozen=True)
class PendingPurge:
    """An entry whose permanent deletion stopped; it stays ``purging`` and is retried."""

    entry_id: str
    reason: str


@dataclass(frozen=True)
class PurgeOutcome:
    purged: tuple[PurgedEntry, ...] = ()
    pending: tuple[PendingPurge, ...] = ()


@dataclass(frozen=True)
class ArchiveListing:
    """One entry as a list shows it.

    ``restorable`` is decided from the entry's kind, state, recorded payload
    format and whether its payload trees exist; a restore still runs the full
    check (:meth:`ArchiveService.restore_check`).
    """

    entry: ArchiveEntry
    label: str
    restorable: bool
    not_restorable_reason: str | None


@dataclass(frozen=True)
class ArchivePage:
    entries: tuple[ArchiveListing, ...]
    next_cursor: ArchiveEntryCursor | None


@dataclass(frozen=True)
class ArchiveEntryDetail:
    """One entry with its first Sessions, its payload state and its restore check.

    ``files`` is ``present`` when every payload tree exists, ``missing`` when
    one is gone and ``none`` for an entry without trees. ``user_folders`` are the
    trees that may be folders the user owns rather than copies vBot made, such
    as a Workspace an older vBot moved into the archive; a purge deletes them too.
    """

    listing: ArchiveListing
    sessions: tuple[ArchiveMember, ...]
    files: str
    restore: RestoreCheck
    user_folders: tuple[ArchiveTree, ...] = ()


__all__ = [
    "RESTORE_CONFLICT_CODES",
    "AgentArchiveOutcome",
    "ArchiveEntryDetail",
    "ArchiveListing",
    "ArchivePage",
    "PendingPurge",
    "ProjectArchiveOutcome",
    "PurgeOutcome",
    "PurgedEntry",
    "RestoreCheck",
    "RestoreOutcome",
    "RestoreProblem",
    "SessionArchiveOutcome",
]
