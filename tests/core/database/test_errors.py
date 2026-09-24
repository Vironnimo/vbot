"""The three database failure kinds and how SQLite failures map onto them."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.chat.errors import ChatSessionError
from core.database import (
    DatabaseCorruptError,
    DatabaseError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    IncidentConflictError,
    open_database,
)
from core.database._connections import classified_error, classify_write_error
from core.sessions.errors import SessionStoreCorruptError
from tests.core.database.database_test_support import note_bodies, notes_spec


def _error(kind: type[sqlite3.Error], message: str, code: int | None = None) -> sqlite3.Error:
    error = kind(message)
    if code is not None:
        error.sqlite_errorcode = code  # type: ignore[attr-defined]
    return error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_error(sqlite3.DatabaseError, "database disk image is malformed"), DatabaseCorruptError),
        (_error(sqlite3.DatabaseError, "file is not a database"), DatabaseCorruptError),
        (_error(sqlite3.DatabaseError, "opaque", sqlite3.SQLITE_CORRUPT), DatabaseCorruptError),
        (_error(sqlite3.DatabaseError, "opaque", sqlite3.SQLITE_NOTADB), DatabaseCorruptError),
        (_error(sqlite3.OperationalError, "database is locked"), DatabaseUnavailableError),
        (
            _error(sqlite3.OperationalError, "attempt to write a readonly database"),
            DatabaseUnavailableError,
        ),
        (_error(sqlite3.OperationalError, "disk I/O error"), DatabaseUnavailableError),
        (_error(sqlite3.OperationalError, "database or disk is full"), DatabaseUnavailableError),
        (
            _error(sqlite3.OperationalError, "opaque", sqlite3.SQLITE_IOERR | (3 << 8)),
            DatabaseUnavailableError,
        ),
    ],
)
def test_sqlite_failures_are_classified_into_the_shared_kinds(
    error: sqlite3.Error, expected: type[DatabaseError]
) -> None:
    translated = classified_error(error, "notes: write failed")

    assert type(translated) is expected
    assert str(translated) == "notes: write failed"


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.IntegrityError("UNIQUE constraint failed: notes.note_id"),
        sqlite3.ProgrammingError("Cannot operate on a closed cursor"),
        sqlite3.OperationalError("no such table: missing"),
        ValueError("not a SQLite failure"),
    ],
)
def test_caller_owned_failures_stay_unclassified(error: Exception) -> None:
    assert classify_write_error(error) == "other"
    assert classified_error(error, "unused") is None


def test_kernel_errors_are_never_domain_errors() -> None:
    for kind in (
        DatabaseError,
        DatabaseUnavailableError,
        DatabaseCorruptError,
        DatabaseFormatError,
        IncidentConflictError,
    ):
        assert not issubclass(kind, ChatSessionError)
    # Owners may add domain context, but the kind stays a storage failure.
    assert issubclass(SessionStoreCorruptError, DatabaseCorruptError)
    assert not issubclass(SessionStoreCorruptError, ChatSessionError)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("database disk image is malformed", DatabaseCorruptError),
        ("attempt to write a readonly database", DatabaseUnavailableError),
    ],
)
def test_a_classified_failure_inside_a_write_rolls_back_and_escapes_as_its_kind(
    data_dir: Path, message: str, expected: type[DatabaseError]
) -> None:
    database = open_database(notes_spec(data_dir))
    try:

        def fail_after_insert(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO notes (body) VALUES ('rolled back')")
            raise sqlite3.DatabaseError(message)

        with pytest.raises(expected) as raised:
            database.write(fail_after_insert)
        assert isinstance(raised.value.__cause__, sqlite3.DatabaseError)
        assert note_bodies(database) == []
        assert not database.writer.in_transaction
    finally:
        database.close()
