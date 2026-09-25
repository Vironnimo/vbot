"""Small database declarations and helpers for kernel tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from core.database import (
    CANONICAL,
    DISPOSABLE,
    Database,
    DatabaseHealth,
    DatabaseSpec,
    Migration,
    SnapshotFacts,
    canonical_database_path,
    create_data_snapshot,
    open_database,
)

#: A test-only application id ("VBTX"), distinct from every real vBot database.
TEST_APPLICATION_ID = 0x56425458

NOTES_SCHEMA_SQL = """
CREATE TABLE notes (
  note_id INTEGER PRIMARY KEY,
  body    TEXT NOT NULL,
  tag     TEXT CHECK (tag IS NULL OR length(tag) <= 40)
) STRICT;

CREATE INDEX notes_by_tag ON notes (tag);
"""

NOTES_FACTS = SnapshotFacts({"note_count": "SELECT COUNT(*) FROM notes"})


def notes_spec(
    data_dir: Path,
    *,
    name: str = "notes",
    schema_sql: str = NOTES_SCHEMA_SQL,
    migrations: tuple[Migration, ...] = (),
    retired_indexes: tuple[str, ...] = (),
    format_generation: int = 1,
    after_open: Callable[[sqlite3.Connection], None] | None = None,
    health: Callable[[sqlite3.Connection], DatabaseHealth] | None = None,
    snapshot_facts: SnapshotFacts | None = NOTES_FACTS,
) -> DatabaseSpec:
    """A canonical database at its fixed path in ``data_dir``."""
    return DatabaseSpec(
        name=name,
        path=canonical_database_path(data_dir, name),
        profile=CANONICAL,
        application_id=TEST_APPLICATION_ID,
        format_generation=format_generation,
        schema_sql=schema_sql,
        migrations=migrations,
        retired_indexes=retired_indexes,
        after_open=after_open,
        snapshot_facts=snapshot_facts,
        health=health,
    )


def projection_spec(
    path: Path,
    *,
    projection_version: int = 1,
    schema_sql: str = NOTES_SCHEMA_SQL,
    format_generation: int = 1,
    connection_setup: Callable[[sqlite3.Connection], None] | None = None,
) -> DatabaseSpec:
    """A disposable database that may live anywhere."""
    return DatabaseSpec(
        name="notes_index",
        path=path,
        profile=DISPOSABLE,
        application_id=TEST_APPLICATION_ID,
        format_generation=format_generation,
        schema_sql=schema_sql,
        projection_version=projection_version,
        connection_setup=connection_setup,
    )


def add_note(database: Database, body: str, tag: str | None = None) -> None:
    database.write(
        lambda connection: connection.execute(
            "INSERT INTO notes (body, tag) VALUES (?, ?)", (body, tag)
        )
    )


def note_bodies(database: Database) -> list[str]:
    with database.read() as connection:
        return [
            str(row[0]) for row in connection.execute("SELECT body FROM notes ORDER BY note_id")
        ]


def stored_bodies(spec: DatabaseSpec) -> list[str]:
    """Open ``spec`` briefly and return its note bodies."""
    database = open_database(spec)
    try:
        return note_bodies(database)
    finally:
        database.close()


def snapshot_with_notes(data_dir: Path, *bodies: str, name: str = "notes") -> Path:
    """Write ``bodies`` to the canonical ``name`` database and snapshot the data directory."""
    database = open_database(notes_spec(data_dir, name=name))
    try:
        for body in bodies:
            add_note(database, body)
        snapshot = create_data_snapshot(data_dir, reason="test", databases=(database,))
    finally:
        database.close()
    assert snapshot is not None
    return snapshot


def raw_execute(path: Path, *statements: str) -> None:
    """Change a closed database file outside the kernel."""
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for statement in statements:
            connection.execute(statement)
    finally:
        connection.close()
