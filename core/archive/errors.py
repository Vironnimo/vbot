"""Refusals of the archive domain; each leaves every archive entry and live resource as it was."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.archive._types import RestoreProblem
from core.sessions import ArchiveEntryBusyError, ArchiveEntryError, ArchiveEntryNotFoundError


class ArchiveRetentionUnknownError(Exception):
    """The retention period cannot be read reliably, such as from an invalid setting.

    Retention then deletes nothing instead of falling back to a default period
    that may be shorter than the one the user chose.
    """


class ArchiveSubjectInUseError(ArchiveEntryError):
    """A Channel or a live automation still targets the Agent, Project or Session to archive.

    ``references`` are the ``kind:id`` labels; ``details`` the automation
    references as ``{"kind", "id", "name"}`` objects (Channels have none).
    """

    def __init__(
        self,
        kind: str,
        subject_id: str,
        references: Sequence[str],
        details: Sequence[Mapping[str, str]] = (),
    ) -> None:
        super().__init__(
            f"cannot archive {kind} {subject_id}: referenced by {', '.join(references)}"
        )
        self.kind = kind
        self.subject_id = subject_id
        self.references = tuple(references)
        self.details = tuple(details)


class ArchiveRestoreConflictError(ArchiveEntryError):
    """The restore target's id or Session addresses are taken; another target id avoids it.

    ``kind`` is the entry's kind, so a caller can say which id the new target id replaces.
    """

    def __init__(self, entry_id: str, kind: str, conflicts: Sequence[RestoreProblem]) -> None:
        super().__init__(
            f"cannot restore archive entry {entry_id}: "
            + "; ".join(conflict.message for conflict in conflicts)
            + "; restore it under another id (target_id)"
        )
        self.entry_id = entry_id
        self.kind = kind
        self.conflicts = tuple(conflicts)


class ArchiveNotRestorableError(ArchiveEntryError):
    """The entry cannot be restored for a reason another target id does not fix."""

    def __init__(self, entry_id: str, blockers: Sequence[RestoreProblem]) -> None:
        super().__init__(
            f"cannot restore archive entry {entry_id}: "
            + "; ".join(blocker.message for blocker in blockers)
        )
        self.entry_id = entry_id
        self.blockers = tuple(blockers)


__all__ = [
    "ArchiveEntryBusyError",
    "ArchiveEntryError",
    "ArchiveEntryNotFoundError",
    "ArchiveNotRestorableError",
    "ArchiveRestoreConflictError",
    "ArchiveSubjectInUseError",
]
