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
    """An archived Identity Agent: its entry, how many Sessions it took, the changed grants.

    ``external_workspace`` is the Workspace folder outside the Agent's directory
    that the archive left in place, if any.
    """

    entry_id: str
    agent_id: str
    session_count: int
    policy_agent_ids: tuple[str, ...]
    external_workspace: str | None = None


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
    """A restored entry: where its Agent, Project or Sessions live now.

    ``grant_agent_ids`` are the Agents whose delegation lists name a restored
    Agent again.
    """

    entry_id: str
    kind: str
    subject_id: str
    target_id: str
    addresses: tuple[SessionAddress, ...]
    warnings: tuple[RestoreProblem, ...]
    grant_agent_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PurgedEntry:
    entry_id: str
    kind: str
    subject_id: str
    session_count: int


@dataclass(frozen=True)
class PendingPurge:
    """An entry whose permanent deletion began and did not finish.

    It stays ``purging`` with what is left, never restorable again; the
    retention sweep continues it, and purging it again continues it at once.
    ``reason`` is ``usage_import_failed``, ``stopped`` (a stop request) or the
    failure's exception class.
    """

    entry_id: str
    reason: str


@dataclass(frozen=True)
class SkippedPurge:
    """An entry a purge left exactly as it was: nothing of it was deleted.

    ``reason`` is ``busy`` when another operation held it (``state`` names that
    operation's entry state, such as ``restoring``), or the exception class of a
    failure that kept the purge from claiming it. Nothing retries it on its own;
    an ``archived`` entry stays restorable.
    """

    entry_id: str
    reason: str
    state: str | None = None


@dataclass(frozen=True)
class PurgeOutcome:
    """What a purge did with each entry it was given.

    ``gone`` names the entries no longer in the archive when the purge reached
    them: another operation deleted or restored them in the meantime.
    """

    purged: tuple[PurgedEntry, ...] = ()
    pending: tuple[PendingPurge, ...] = ()
    skipped: tuple[SkippedPurge, ...] = ()
    gone: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchiveListing:
    """One entry as a list shows it.

    ``restorable`` is decided from the entry's kind, state, recorded payload
    format and whether its payload trees exist; a restore still runs the full
    check (:meth:`ArchiveService.restore_check`). ``purge_at`` is when the
    retention period ends for the entry, ``None`` when retention never deletes it.
    """

    entry: ArchiveEntry
    label: str
    restorable: bool
    not_restorable_reason: str | None
    purge_at: str | None = None


@dataclass(frozen=True)
class ArchivePage:
    """One page of entries and the retention period in days (``None``: kept until deleted)."""

    entries: tuple[ArchiveListing, ...]
    next_cursor: ArchiveEntryCursor | None
    retention_days: int | None = None


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
    "SkippedPurge",
]
