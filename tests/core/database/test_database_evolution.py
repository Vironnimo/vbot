"""The evolution contract: additive reconcile, migration ledger and format generations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    DatabaseConversionRequiredError,
    DatabaseFormatError,
    DatabaseSchemaMismatchError,
    Migration,
    open_database,
    open_offline_database,
    read_marker,
    write_marker_for_databases,
)
from core.database.recovery import quarantine_root
from tests.core.database.database_test_support import (
    NOTES_SCHEMA_SQL,
    add_note,
    note_bodies,
    notes_spec,
    raw_execute,
    rewrite_marker,
)


def _created(data_dir: Path, *bodies: str) -> Path:
    """Create the notes database with ``bodies`` and close it."""
    database = open_database(notes_spec(data_dir))
    try:
        for body in bodies:
            add_note(database, body)
    finally:
        database.close()
    return database.path


def _columns(path: Path, table: str = "notes") -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _ledger(path: Path) -> dict[str, tuple[int, str]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            str(name): (int(breaks_older), str(version))
            for name, breaks_older, version in connection.execute(
                "SELECT name, breaks_older, applied_by_version FROM kernel_migrations"
            )
        }


# Table constraints written without a space must parse like spaced ones.
_COMPACT_SCHEMA_SQL = NOTES_SCHEMA_SQL + (
    "\nCREATE TABLE pairs(left_id TEXT NOT NULL,right_id TEXT NOT NULL,"
    "flag INTEGER NOT NULL CHECK(flag IN(0,1)),"
    "PRIMARY KEY(left_id,right_id),UNIQUE(right_id,left_id)) STRICT;"
)


def test_the_current_schema_reopens_without_a_change_while_another_writer_holds_admission(
    data_dir: Path,
) -> None:
    spec = notes_spec(data_dir, schema_sql=_COMPACT_SCHEMA_SQL)
    open_database(spec).close()

    # A planned change would need the write lock the other writer holds.
    with closing(sqlite3.connect(spec.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            open_database(spec).close()
        finally:
            writer.rollback()


def test_open_adds_missing_columns_tables_and_indexes_and_keeps_rows(data_dir: Path) -> None:
    _created(data_dir, "kept")
    grown = NOTES_SCHEMA_SQL + (
        "\nALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0 "
        "CHECK (pinned IN (0, 1));"
        "\nCREATE TABLE labels (label TEXT PRIMARY KEY) STRICT;"
        "\nCREATE INDEX notes_by_pinned ON notes (pinned, note_id);"
    )

    database = open_database(notes_spec(data_dir, schema_sql=grown))
    try:
        assert note_bodies(database) == ["kept"]
        with database.read() as connection:
            assert connection.execute("SELECT pinned FROM notes").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'notes_by_pinned'"
                ).fetchone()
                is not None
            )
    finally:
        database.close()
    # The added objects match their declaration exactly.
    open_offline_database(notes_spec(data_dir, schema_sql=grown)).close()


def _replace_table(schema_sql: str, old: str, new: str) -> str:
    assert old in schema_sql
    return schema_sql.replace(old, new)


_TAG = "  tag     TEXT CHECK (tag IS NULL OR length(tag) <= 40)\n"


@pytest.mark.parametrize(
    ("changed_schema", "message"),
    [
        (
            _replace_table(NOTES_SCHEMA_SQL, _TAG, _TAG[:-1] + ",\n  required TEXT NOT NULL\n"),
            "cannot be added",
        ),
        (
            _replace_table(
                NOTES_SCHEMA_SQL,
                _TAG,
                _TAG[:-1] + ",\n  size    INTEGER GENERATED ALWAYS AS (length(body)) VIRTUAL\n",
            ),
            "cannot be added",
        ),
        (
            _replace_table(
                NOTES_SCHEMA_SQL, "note_id INTEGER PRIMARY KEY", "note_id INTEGER NOT NULL"
            ).replace("body    TEXT NOT NULL", "body    TEXT PRIMARY KEY NOT NULL"),
            "primary key",
        ),
        (
            _replace_table(NOTES_SCHEMA_SQL, "length(tag) <= 40", "length(tag) <= 41"),
            "declared shape",
        ),
        (_replace_table(NOTES_SCHEMA_SQL, ") STRICT;", ");"), "options"),
        (
            _replace_table(NOTES_SCHEMA_SQL, "ON notes (tag);", "ON notes (tag, note_id);"),
            "needs a new name",
        ),
        (
            NOTES_SCHEMA_SQL + "\nCREATE VIEW tagged AS SELECT note_id FROM notes WHERE tag;",
            "view tagged",
        ),
    ],
    ids=[
        "required-column",
        "generated-column",
        "primary-key",
        "column-contract",
        "table-options",
        "index-definition",
        "view-definition",
    ],
)
def test_reconcile_refuses_every_non_additive_change_untouched(
    data_dir: Path, changed_schema: str, message: str
) -> None:
    path = _created(data_dir, "kept")
    if "CREATE VIEW" in changed_schema:
        raw_execute(path, "CREATE VIEW tagged AS SELECT note_id FROM notes WHERE tag IS NOT NULL")
    original = path.read_bytes()

    with pytest.raises(DatabaseSchemaMismatchError, match=message):
        open_offline_database(notes_spec(data_dir, schema_sql=changed_schema))

    assert path.read_bytes() == original


def test_an_older_declaration_tolerates_newer_columns_tables_indexes_and_triggers(
    data_dir: Path,
) -> None:
    path = _created(data_dir, "kept")
    raw_execute(
        path,
        "ALTER TABLE notes ADD COLUMN newer_flag INTEGER NOT NULL DEFAULT 1",
        "CREATE TABLE newer_table (value TEXT NOT NULL) STRICT",
        "CREATE INDEX newer_index ON notes (newer_flag)",
        "CREATE TRIGGER newer_trigger AFTER INSERT ON notes BEGIN SELECT 1; END",
    )

    database = open_database(notes_spec(data_dir))
    try:
        add_note(database, "written by the older declaration")
        assert note_bodies(database) == ["kept", "written by the older declaration"]
    finally:
        database.close()


def test_open_drops_retired_indexes_that_are_no_longer_declared(data_dir: Path) -> None:
    with_index = NOTES_SCHEMA_SQL + "\nCREATE INDEX notes_by_body ON notes (body);"
    open_database(notes_spec(data_dir, schema_sql=with_index)).close()

    with pytest.raises(ValueError, match="retired indexes are still declared"):
        open_database(
            notes_spec(data_dir, schema_sql=with_index, retired_indexes=("notes_by_body",))
        )
    database = open_database(notes_spec(data_dir, retired_indexes=("notes_by_body",)))
    try:
        with database.read() as connection:
            names = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
    finally:
        database.close()
    assert "notes_by_body" not in names
    assert "notes_by_tag" in names
    # An absent retired index is simply not there.
    open_database(notes_spec(data_dir, retired_indexes=("notes_by_body",))).close()


def test_reconcile_rolls_back_all_ddl_when_a_later_statement_fails(data_dir: Path) -> None:
    path = _created(data_dir)
    raw_execute(
        path,
        "CREATE TABLE duplicate_values (value TEXT) STRICT",
        "INSERT INTO duplicate_values (value) VALUES ('same'), ('same')",
    )
    future = NOTES_SCHEMA_SQL + (
        "\nCREATE TABLE duplicate_values (value TEXT) STRICT;"
        "\nCREATE TABLE new_table (value TEXT) STRICT;"
        "\nCREATE UNIQUE INDEX unique_values ON duplicate_values (value);"
    )

    with pytest.raises(
        DatabaseSchemaMismatchError, match="index unique_values cannot be created over the existing"
    ):
        open_offline_database(notes_spec(data_dir, schema_sql=future))

    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name = 'new_table'").fetchone()
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM duplicate_values").fetchone()[0] == 2


def test_reconcile_plans_again_after_another_opener_commits(data_dir: Path) -> None:
    path = _created(data_dir)
    future = notes_spec(
        data_dir,
        schema_sql=NOTES_SCHEMA_SQL
        + "\nALTER TABLE notes ADD COLUMN concurrent INTEGER NOT NULL DEFAULT 0;",
    )
    competing_open = False

    def authorize(action: int, operation: Any, *_arguments: Any) -> int:
        nonlocal competing_open
        if action == sqlite3.SQLITE_TRANSACTION and operation == "BEGIN" and not competing_open:
            # Another opener evolves the file between this one's plan and its lock.
            competing_open = True
            open_offline_database(future).close()
        return sqlite3.SQLITE_OK

    def race_the_first_transaction(connection: sqlite3.Connection) -> None:
        connection.set_authorizer(authorize)

    open_database(replace(future, connection_setup=race_the_first_transaction)).close()

    assert competing_open
    assert "concurrent" in _columns(path)


def test_a_fresh_database_records_every_declared_migration_without_running_it(
    data_dir: Path,
) -> None:
    def refuse(_connection: sqlite3.Connection) -> None:
        raise AssertionError("a fresh database must not run migrations")

    migrations = (
        Migration("notes.first", apply=refuse),
        Migration("notes.second", breaks_older=True, apply=refuse),
    )

    database = open_database(notes_spec(data_dir, migrations=migrations))
    database.close()

    ledger = _ledger(database.path)
    assert set(ledger) == {"notes.first", "notes.second"}
    assert ledger["notes.first"][0] == 0
    assert ledger["notes.second"][0] == 1


def test_a_new_migration_runs_once_with_the_additive_reconcile(data_dir: Path) -> None:
    _created(data_dir, "untagged")
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
    _created(data_dir)

    def fail(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("injected migration failure")

    grown = NOTES_SCHEMA_SQL + "\nALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;"
    spec = notes_spec(data_dir, schema_sql=grown, migrations=(Migration("notes.fail", apply=fail),))
    with pytest.raises(RuntimeError, match="injected migration failure"):
        open_database(spec)

    assert "pinned" not in _columns(spec.path)
    assert "notes.fail" not in _ledger(spec.path)


def test_an_older_binary_reads_additive_growth_and_refuses_a_breaking_change(
    data_dir: Path,
) -> None:
    """A newer vBot grows the database; this version keeps working until a breaking step."""
    older = notes_spec(data_dir)
    _created(data_dir, "before the update")

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


@pytest.mark.parametrize("file_generation", [2, 0])
def test_a_file_of_another_format_generation_is_refused_untouched(
    data_dir: Path, file_generation: int
) -> None:
    path = _created(data_dir)
    raw_execute(
        path,
        f"PRAGMA user_version = {file_generation}",
        f"UPDATE kernel_meta SET value = '{file_generation}' WHERE key = 'format_generation'",
    )
    original = path.read_bytes()

    with pytest.raises(DatabaseFormatError) as refused:
        open_offline_database(notes_spec(data_dir))
    # Older data predates this vBot's format; newer data needs a newer vBot.
    assert isinstance(refused.value, DatabaseConversionRequiredError) is (file_generation == 0)
    with pytest.raises(DatabaseFormatError) as refused:
        open_database(notes_spec(data_dir))
    assert isinstance(refused.value, DatabaseConversionRequiredError) is (file_generation == 0)
    if isinstance(refused.value, DatabaseConversionRequiredError):
        assert refused.value.database == "notes"
        assert refused.value.data_dir == data_dir.resolve()
    assert path.read_bytes() == original


def test_a_marker_entry_of_another_generation_is_refused_before_the_file_is_read(
    data_dir: Path,
) -> None:
    path = _created(data_dir)
    path.write_bytes(b"not even a database")

    with pytest.raises(DatabaseConversionRequiredError) as refused:
        open_database(notes_spec(data_dir, format_generation=2))
    assert refused.value.database == "notes"

    def newer(payload: dict[str, Any]) -> None:
        payload["databases"]["notes"]["format_generation"] = 2

    rewrite_marker(data_dir, newer)
    with pytest.raises(DatabaseFormatError, match="newer than this vBot supports"):
        open_database(notes_spec(data_dir))
    assert path.read_bytes() == b"not even a database"


def test_the_offline_api_builds_databases_the_marker_then_lists(
    data_dir: Path, tmp_path: Path
) -> None:
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
    # A listed file must sit at the canonical path of the name it records.
    with pytest.raises(DatabaseFormatError, match="canonical path"):
        write_marker_for_databases(data_dir, [database.path])
    current = read_marker(data_dir)
    assert current is not None
    assert current.databases == {}
