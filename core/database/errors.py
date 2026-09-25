"""Failure types shared by every vBot SQLite database.

Owners may subclass these for domain context; callers and RPC map the three
kinds uniformly. None of them inherits a domain error, so a storage failure is
never mistaken for a domain outcome such as a missing Session.
"""

from __future__ import annotations

from pathlib import Path

from core.utils.errors import VBotError

#: The offline converter that brings a data directory from before persistence
#: Generation 1 to the current format. It ships only with a vBot source checkout.
GENERATION_1_CONVERTER_COMMAND = "python -m scripts.converters.persistence_generation_1"


def generation_1_conversion_hint(data_dir: Path | None = None) -> str:
    """Return the one next step for data from before persistence Generation 1.

    Every refusal of such data ends with this text. Without ``data_dir`` the
    converter command names a ``<data-dir>`` placeholder.
    """
    return (
        "Data written by a vBot before persistence Generation 1 (0.4.x) must be converted "
        "once, offline, with vBot stopped: in a vBot source checkout (installed builds do "
        f"not include the converter), run `{_converter_command(data_dir)}`. Follow the "
        'procedure in USAGE.md of the vBot repository, section "Converting an existing data '
        'directory", which starts with a backup and a dry run'
    )


def _converter_command(data_dir: Path | None) -> str:
    return f"{GENERATION_1_CONVERTER_COMMAND} {data_dir if data_dir is not None else '<data-dir>'}"


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
    unreadable marker, an incomplete maintenance operation, (as
    :class:`DatabaseConversionRequiredError`) an older format generation or a
    data directory without a marker, and (as
    :class:`DatabaseSchemaMismatchError`) a schema this vBot cannot reconcile.
    Never grounds to quarantine or restore.
    """


class DatabaseConversionRequiredError(DatabaseFormatError):
    """Data from an older vBot that only the offline converter can make current.

    Raised for a database of an older format generation and for an existing
    data directory without a data-store marker, which is what every data
    directory from before persistence Generation 1 looks like. The message
    names the converter command for ``data_dir`` and where it is documented.
    """

    def __init__(self, problem: str, *, data_dir: Path | None, database: str | None = None) -> None:
        self.database = database
        self.data_dir = data_dir
        self.converter_command = _converter_command(data_dir)
        super().__init__(f"{problem}. {generation_1_conversion_hint(data_dir)}")


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
