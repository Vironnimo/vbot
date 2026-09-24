"""Declarative additive reconcile, retired indexes and schema refusal."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from core.database import DatabaseCorruptError, open_database, open_offline_database
from core.database._schema import KERNEL_SCHEMA_SQL, declared_schema, schema_changes
from core.database.database import _evolve
from tests.core.database.database_test_support import (
    NOTES_SCHEMA_SQL,
    add_note,
    note_bodies,
    notes_spec,
    raw_execute,
)


def _declared(schema_sql: str):
    return declared_schema(KERNEL_SCHEMA_SQL + schema_sql)


def _created(data_dir: Path, *bodies: str) -> Path:
    """Create the notes database with ``bodies`` and close it."""
    database = open_database(notes_spec(data_dir))
    try:
        for body in bodies:
            add_note(database, body)
    finally:
        database.close()
    return database.path


def _changes(path: Path, schema_sql: str, *, retired: tuple[str, ...] = ()) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [
            description
            for _statement, description in schema_changes(
                connection, _declared(schema_sql), retired_indexes=retired
            )
        ]


def test_declared_schema_marks_generated_and_key_columns_not_addable() -> None:
    declared = _declared(
        "CREATE TABLE shapes (\n"
        "  shape_id INTEGER PRIMARY KEY,\n"
        "  width    INTEGER NOT NULL,\n"
        "  area     INTEGER GENERATED ALWAYS AS (width * width) VIRTUAL,\n"
        "  label    TEXT NOT NULL DEFAULT ''\n"
        ") STRICT;"
    )
    columns = declared.table_columns["shapes"]

    assert columns["shape_id"].addable is False
    assert columns["width"].addable is False
    assert columns["area"].addable is False
    assert columns["label"].addable is True
    assert set(declared.table_columns) >= {"kernel_meta", "kernel_migrations", "shapes"}


def test_reconcile_is_a_noop_on_the_current_schema(data_dir: Path) -> None:
    path = _created(data_dir)

    assert _changes(path, NOTES_SCHEMA_SQL) == []


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
    assert _changes(database.path, grown) == []


def test_reconcile_plans_additions_without_writing(data_dir: Path) -> None:
    path = _created(data_dir)
    grown = NOTES_SCHEMA_SQL + "\nALTER TABLE notes ADD COLUMN extra TEXT;"

    assert _changes(path, grown) == ["added column notes.extra"]
    assert _changes(path, grown) == ["added column notes.extra"]


def _replace_table(schema_sql: str, old: str, new: str) -> str:
    assert old in schema_sql
    return schema_sql.replace(old, new)


@pytest.mark.parametrize(
    ("changed_schema", "message"),
    [
        (
            _replace_table(
                NOTES_SCHEMA_SQL,
                "  tag     TEXT CHECK (tag IS NULL OR length(tag) <= 40)\n",
                "  tag     TEXT CHECK (tag IS NULL OR length(tag) <= 40),\n"
                "  required TEXT NOT NULL\n",
            ),
            "cannot be added",
        ),
        (
            _replace_table(
                NOTES_SCHEMA_SQL,
                "  tag     TEXT CHECK (tag IS NULL OR length(tag) <= 40)\n",
                "  tag     TEXT CHECK (tag IS NULL OR length(tag) <= 40),\n"
                "  size    INTEGER GENERATED ALWAYS AS (length(body)) VIRTUAL\n",
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
            _replace_table(NOTES_SCHEMA_SQL, "body    TEXT NOT NULL", "body    BLOB NOT NULL"),
            "declared shape",
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
            _replace_table(
                NOTES_SCHEMA_SQL, "CREATE INDEX notes_by_tag", "CREATE UNIQUE INDEX notes_by_tag"
            ),
            "needs a new name",
        ),
    ],
    ids=[
        "required-column",
        "generated-column",
        "primary-key",
        "column-type",
        "column-contract",
        "table-options",
        "index-definition",
        "unique-index",
    ],
)
def test_reconcile_refuses_every_non_additive_change(
    data_dir: Path, changed_schema: str, message: str
) -> None:
    path = _created(data_dir, "kept")
    original = path.read_bytes()

    with pytest.raises(DatabaseCorruptError, match=message):
        _changes(path, changed_schema)
    with pytest.raises(DatabaseCorruptError):
        open_offline_database(notes_spec(data_dir, schema_sql=changed_schema))

    assert path.read_bytes() == original


def test_reconcile_refuses_a_changed_view_or_trigger(data_dir: Path) -> None:
    with_view = NOTES_SCHEMA_SQL + "\nCREATE VIEW tagged AS SELECT note_id FROM notes WHERE tag;"
    database = open_database(notes_spec(data_dir, schema_sql=with_view))
    database.close()

    changed = with_view.replace("WHERE tag;", "WHERE tag IS NOT NULL;")
    with pytest.raises(DatabaseCorruptError, match="view tagged"):
        _changes(database.path, changed)


def test_older_declaration_tolerates_newer_columns_tables_and_indexes(data_dir: Path) -> None:
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
    assert _changes(path, NOTES_SCHEMA_SQL) == []


def test_open_drops_retired_indexes(data_dir: Path) -> None:
    with_index = NOTES_SCHEMA_SQL + "\nCREATE INDEX notes_by_body ON notes (body);"
    open_database(notes_spec(data_dir, schema_sql=with_index)).close()
    path = notes_spec(data_dir).path

    assert _changes(path, NOTES_SCHEMA_SQL, retired=("notes_by_body",)) == [
        "dropped retired index notes_by_body"
    ]
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


def test_a_retired_index_must_not_still_be_declared(data_dir: Path) -> None:
    with pytest.raises(ValueError, match="retired indexes are still declared"):
        open_database(notes_spec(data_dir, retired_indexes=("notes_by_tag",)))


def test_reconcile_rolls_back_all_ddl_when_a_later_statement_fails(data_dir: Path) -> None:
    path = _created(data_dir)
    raw_execute(
        path,
        "CREATE TABLE duplicate_values (value TEXT) STRICT",
        "INSERT INTO duplicate_values VALUES ('same'), ('same')",
    )
    future = NOTES_SCHEMA_SQL + (
        "\nCREATE TABLE duplicate_values (value TEXT) STRICT;"
        "\nCREATE TABLE new_table (value TEXT) STRICT;"
        "\nCREATE UNIQUE INDEX unique_values ON duplicate_values (value);"
    )

    with pytest.raises(DatabaseCorruptError):
        open_offline_database(notes_spec(data_dir, schema_sql=future))

    with closing(sqlite3.connect(path)) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name = 'new_table'").fetchone()
            is None
        )
        assert connection.execute("SELECT COUNT(*) FROM duplicate_values").fetchone()[0] == 2


def test_reconcile_plans_again_after_another_opener_commits(data_dir: Path) -> None:
    path = _created(data_dir)
    future = (
        NOTES_SCHEMA_SQL + "\nALTER TABLE notes ADD COLUMN concurrent INTEGER NOT NULL DEFAULT 0;"
    )
    spec = notes_spec(data_dir, schema_sql=future)
    declared = _declared(future)
    competing_open = False

    def authorize(action, operation, _table, _database, _trigger):
        nonlocal competing_open
        if action == sqlite3.SQLITE_TRANSACTION and operation == "BEGIN" and not competing_open:
            competing_open = True
            with closing(sqlite3.connect(path, isolation_level=None)) as other:
                _evolve(other, spec, declared)
        return sqlite3.SQLITE_OK

    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.set_authorizer(authorize)
        _evolve(connection, spec, declared)
        connection.set_authorizer(None)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(notes)")}

    assert competing_open
    assert "concurrent" in columns


def test_current_schema_opens_while_another_writer_holds_admission(data_dir: Path) -> None:
    path = _created(data_dir)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            open_database(notes_spec(data_dir)).close()
        finally:
            writer.rollback()
