"""Failure types shared by every vBot SQLite database.

Owners may subclass these for domain context; callers and RPC map the three
kinds uniformly. None of them inherits a domain error, so a storage failure is
never mistaken for a domain outcome such as a missing Session.
"""

from __future__ import annotations

from core.utils.errors import VBotError


class DatabaseError(VBotError):
    """Base error for an unsafe or unusable database state."""


class DatabaseUnavailableError(DatabaseError):
    """The database cannot complete an operation right now.

    Busy, locked, permission, disk-full and transient I/O failures, and a
    contended data-store operation lock. Never grounds to discard or
    quarantine a database.
    """


class DatabaseCorruptError(DatabaseError):
    """The database, or a snapshot or recovery record of it, cannot be trusted.

    Classified SQLite corruption, a schema that differs from the declaration,
    and identity mismatches.
    """


class DatabaseFormatError(DatabaseError):
    """The data directory or database does not authorize this Runtime to open it.

    A newer or unknown format generation, an unknown migration that declares
    older versions cannot read the result, a missing or unreadable marker, and
    an incomplete maintenance operation. Never grounds to quarantine.
    """


class IncidentConflictError(DatabaseError):
    """An acknowledgement refers to a recovery incident superseded on disk."""


class UpdateRollbackRefusedError(DatabaseError):
    """An automatic update rollback cannot prove its data snapshot is safe to restore.

    Nothing was changed. The snapshot is missing, belongs to another update, no
    longer verifies, or the data directory may have been written by something
    other than the failed candidate since the snapshot.
    """
