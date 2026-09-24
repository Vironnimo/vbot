"""Migration ledger, format generations and the older-binary contract."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from core.database import (
    DatabaseFormatError,
    MarkerEntry,
    Migration,
    open_database,
    open_offline_database,
    read_marker,
)
from core.database.marker import write_marker_for_databases
from core.database.recovery import quarantine_root
from tests.core.database.database_test_support import (
    NOTES_SCHEMA_SQL,
    add_note,
    note_bodies,
    notes_spec,
    raw_execute,
)


def _ledger(path: Path) -> dict[str, tuple[int, str]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            str(name): (int(breaks_older), str(version))
            for name, breaks_older, version in connection.execute(
                "SELECT name, breaks_older, applied_by_version FROM kernel_migrations"
            )
        }


def _refuse(_connection: sqlite3.Connection) -> None:
    raise AssertionError("a fresh database must not run migrations")


def test_fresh_database_records_every_declared_migration_without_running_it(
    data_dir: Path,
) -> None:
    migrations = (
        Migration("notes.first", apply=_refuse),
        Migration("notes.second", breaks_older=True, apply=_refuse),
    )

    database = open_database(notes_spec(data_dir, migrations=migrations))
    database.close()

    ledger = _ledger(database.path)
    assert set(ledger) == {"notes.first", "notes.second"}
    assert ledger["notes.first"][0] == 0
    assert ledger["notes.second"][0] == 1


def test_a_new_migration_runs_once_with_the_additive_reconcile(data_dir: Path) -> None:
    database = open_database(notes_spec(data_dir))
    add_note(database, "untagged")
    database.close()
    calls = 0

    def backfill(connection: sqlite3.Connection) -> None:
        nonlocal calls
        calls += 1
        # The added column already exists inside the same transaction.
        connection.execute("UPDATE notes SET pinned = 1 WHERE pinned = 0")

    grown = NOTES_SCHEMA_SQL + "\nALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;"
    spec = notes_spec(
        data_dir, schema_sql=grown, migrations=(Migration("notes.pin_all", apply=backfill),)
    )
    for _ in range(2):
        open_database(spec).close()

    assert calls == 1
    ledger = _ledger(spec.path)
    assert ledger["notes.pin_all"][0] == 0
    assert ledger["notes.pin_all"][1]
    with closing(sqlite3.connect(spec.path)) as connection:
        assert connection.execute("SELECT pinned FROM notes").fetchone()[0] == 1


def test_a_failing_migration_rolls_back_its_reconcile(data_dir: Path) -> None:
    open_database(notes_spec(data_dir)).close()

    def fail(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("injected migration failure")

    grown = NOTES_SCHEMA_SQL + "\nALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;"
    spec = notes_spec(data_dir, schema_sql=grown, migrations=(Migration("notes.fail", apply=fail),))
    with pytest.raises(RuntimeError, match="injected migration failure"):
        open_database(spec)

    with closing(sqlite3.connect(spec.path)) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(notes)")}
    assert "pinned" not in columns
    assert "notes.fail" not in _ledger(spec.path)


@pytest.mark.parametrize("breaks_older", [False, True])
def test_unknown_ledger_rows_refuse_only_when_they_break_older_versions(
    data_dir: Path, breaks_older: bool
) -> None:
    path = notes_spec(data_dir).path
    open_database(notes_spec(data_dir)).close()
    raw_execute(
        path,
        "INSERT INTO kernel_migrations (name, applied_at, applied_by_version, breaks_older) "
        f"VALUES ('notes.from_the_future', '2027-01-01T00:00:00Z', '9.0.0', {int(breaks_older)})",
    )
    original = path.read_bytes()

    if breaks_older:
        with pytest.raises(DatabaseFormatError, match="notes.from_the_future"):
            open_database(notes_spec(data_dir))
        assert path.read_bytes() == original
        assert not quarantine_root(data_dir).exists()
    else:
        open_database(notes_spec(data_dir)).close()


def test_older_binary_simulation_reads_additive_growth_and_refuses_a_breaking_change(
    data_dir: Path,
) -> None:
    """A newer vBot grows the database; this version keeps working until a breaking step."""
    older = notes_spec(data_dir)
    database = open_database(older)
    add_note(database, "before the update")
    database.close()

    newer_schema = NOTES_SCHEMA_SQL + (
        "\nALTER TABLE notes ADD COLUMN color TEXT;"
        "\nCREATE TABLE note_links (source INTEGER NOT NULL, target INTEGER NOT NULL) STRICT;"
    )
    newer = notes_spec(
        data_dir,
        schema_sql=newer_schema,
        migrations=(Migration("notes.colors", apply=lambda _connection: None),),
    )
    database = open_database(newer)
    database.write(
        lambda connection: connection.execute(
            "INSERT INTO notes (body, color) VALUES ('written by the newer vBot', 'red')"
        )
    )
    database.close()

    # Rollback to the older binary: extra column, extra table and an unknown
    # non-breaking ledger row are all tolerated.
    database = open_database(older)
    try:
        add_note(database, "after the rollback")
        assert note_bodies(database) == [
            "before the update",
            "written by the newer vBot",
            "after the rollback",
        ]
    finally:
        database.close()

    breaking = notes_spec(
        data_dir,
        schema_sql=newer_schema,
        migrations=(
            Migration("notes.colors"),
            Migration("notes.split_bodies", breaks_older=True),
        ),
    )
    open_database(breaking).close()
    original = older.path.read_bytes()

    with pytest.raises(DatabaseFormatError, match="notes.split_bodies"):
        open_database(older)
    assert older.path.read_bytes() == original
    assert not quarantine_root(data_dir).exists()


@pytest.mark.parametrize(
    ("file_generation", "message"),
    [(2, "newer than this vBot supports"), (0, "Run the converter first")],
)
def test_a_file_of_another_format_generation_is_refused_untouched(
    data_dir: Path, file_generation: int, message: str
) -> None:
    open_database(notes_spec(data_dir)).close()
    path = notes_spec(data_dir).path
    raw_execute(
        path,
        f"PRAGMA user_version = {file_generation}",
        f"UPDATE kernel_meta SET value = '{file_generation}' WHERE key = 'format_generation'",
    )
    original = path.read_bytes()

    with pytest.raises(DatabaseFormatError, match=message):
        open_offline_database(notes_spec(data_dir))
    assert path.read_bytes() == original


def test_a_marker_entry_of_a_newer_generation_is_refused_before_the_file_is_read(
    data_dir: Path,
) -> None:
    open_database(notes_spec(data_dir)).close()
    marker = read_marker(data_dir)
    assert marker is not None
    entry = marker.databases["notes"]
    from core.database.marker import _write_marker

    _write_marker(data_dir, {"notes": MarkerEntry(entry.database_id, 2)})
    path = notes_spec(data_dir).path
    path.write_bytes(b"not even a database")

    with pytest.raises(DatabaseFormatError, match="newer than this vBot supports"):
        open_database(notes_spec(data_dir))
    assert path.read_bytes() == b"not even a database"


def test_a_newer_generation_spec_refuses_an_older_database(data_dir: Path) -> None:
    open_database(notes_spec(data_dir)).close()

    with pytest.raises(DatabaseFormatError, match="Run the converter first"):
        open_database(notes_spec(data_dir, format_generation=2))


def test_offline_api_builds_a_registered_database_outside_the_marker(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    database = open_offline_database(notes_spec(staging))
    try:
        add_note(database, "converted")
    finally:
        database.close()

    marker = write_marker_for_databases(staging, [database.path])

    assert marker.databases["notes"].database_id == database.database_id
    assert marker.databases["notes"].format_generation == 1
    assert read_marker(staging) == marker
