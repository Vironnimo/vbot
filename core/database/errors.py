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

    Classified SQLite corruption (``SQLITE_CORRUPT``, ``SQLITE_NOTADB``,
    ``SQLITE_FORMAT``), failed ``quick_check`` or ``foreign_key_check``, a
    missing or foreign kernel identity, and a file whose identity differs from
    the one the data-store marker registers. The only kind of failure that may
    ground quarantine and automatic restore.
    """


class DatabaseFormatError(DatabaseError):
    """The data directory or database does not authorize this Runtime to open it.

    A newer or unknown format generation, an unknown migration that declares
    older versions cannot read the result, a migration that fails, a missing or
    unreadable marker, an incomplete maintenance operation, and (as
    :class:`DatabaseSchemaMismatchError`) a schema this vBot cannot reconcile.
    Never grounds to quarantine or restore.
    """


class DatabaseSchemaMismatchError(DatabaseFormatError):
    """A database differs from the declared schema in a way no open may repair.

    Raised for a live object whose shape the additive reconcile cannot express,
    a declared change that cannot be applied to the existing rows, a declared
    relation or snapshot fact that cannot be read, and a live definition that
    cannot be parsed. The file is intact and left unchanged: this is a
    compatibility problem between the data and this vBot, never grounds to
    quarantine or restore.
    """

    def __init__(self, database: str, object_name: str, difference: str) -> None:
        self.database = database
        self.object_name = object_name
        self.difference = difference
        super().__init__(
            f"the {database} database does not match the schema this vBot declares: "
            f"{object_name} {difference}. Within one format generation"
            " only additive changes are allowed (new tables, views, indexes and "
            "triggers, and new columns that are nullable or have a default); a changed "
            "object needs a new name, any other change a new format generation with a "
            "converter"
        )


class IncidentConflictError(DatabaseError):
    """An acknowledgement refers to a recovery incident superseded on disk."""


class UpdateRollbackRefusedError(DatabaseError):
    """An automatic update rollback cannot prove its data snapshot is safe to restore.

    Nothing was changed. The snapshot is missing, belongs to another update, no
    longer verifies, or the data directory may have been written by something
    other than the failed candidate since the snapshot.
    """
