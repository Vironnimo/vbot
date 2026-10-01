"""core.archive — archive entries: archive, restore and permanent deletion of Agents,
Projects and Sessions."""

from core.archive._types import (
    RESTORE_CONFLICT_CODES,
    AgentArchiveOutcome,
    ArchiveEntryDetail,
    ArchiveListing,
    ArchivePage,
    PendingPurge,
    ProjectArchiveOutcome,
    PurgedEntry,
    PurgeOutcome,
    RestoreCheck,
    RestoreOutcome,
    RestoreProblem,
    SessionArchiveOutcome,
    SkippedPurge,
)
from core.archive.archive import ArchiveService, ArchiveServices
from core.archive.errors import (
    ArchiveEntryBusyError,
    ArchiveEntryError,
    ArchiveEntryNotFoundError,
    ArchiveNotRestorableError,
    ArchiveRestoreConflictError,
    ArchiveSubjectInUseError,
)

__all__ = [
    "RESTORE_CONFLICT_CODES",
    "AgentArchiveOutcome",
    "ArchiveEntryBusyError",
    "ArchiveEntryDetail",
    "ArchiveEntryError",
    "ArchiveEntryNotFoundError",
    "ArchiveListing",
    "ArchiveNotRestorableError",
    "ArchivePage",
    "ArchiveRestoreConflictError",
    "ArchiveService",
    "ArchiveServices",
    "ArchiveSubjectInUseError",
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
