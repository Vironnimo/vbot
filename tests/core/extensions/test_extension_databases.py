"""Owner-bound Extension databases: naming, registration, lifetime and admission."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.database import APPLICATION_IDS, read_marker, write_bootstrap_marker
from core.extensions import ExtensionRegistrationIdentity
from core.extensions._declarations import ExtensionUnavailableError
from core.extensions.databases import (
    ExtensionDatabases,
    Migration,
    extension_database_name,
    extension_database_spec,
)

SCHEMA = "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL) STRICT;"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    write_bootstrap_marker(tmp_path)
    return tmp_path


@pytest.fixture
def databases(data_dir: Path):
    owner = ExtensionDatabases(data_dir)
    yield owner
    owner.close()


def _identity(name: str = "notes_ext", epoch: str = "epoch-1") -> ExtensionRegistrationIdentity:
    return ExtensionRegistrationIdentity(name, epoch)


def _insert(database, body: str) -> None:
    database.write(
        lambda connection: connection.execute("INSERT INTO notes (body) VALUES (?)", (body,))
    )


def _bodies(database) -> list[str]:
    with database.read() as connection:
        return [row["body"] for row in connection.execute("SELECT body FROM notes ORDER BY id")]


def test_open_creates_a_registered_canonical_extension_database(databases, data_dir) -> None:
    identity = _identity()

    database = asyncio.run(databases.opener(identity)("notes", SCHEMA))

    assert database.name == "ext.notes_ext.notes"
    assert database.path == data_dir / "extension-data" / "notes_ext" / "notes.db"
    marker = read_marker(data_dir)
    assert marker is not None
    assert marker.databases["ext.notes_ext.notes"].database_id == database.database_id
    assert marker.databases["ext.notes_ext.notes"].format_generation == 1
    with database.read() as connection:
        assert (
            connection.execute("PRAGMA application_id").fetchone()[0]
            == (APPLICATION_IDS["extensions"])
        )
    assert databases.open_databases() == (database,)


def test_release_closes_the_owner_handles_and_a_new_registration_reopens(
    databases, data_dir
) -> None:
    first_owner, other_owner = _identity(), _identity("other_ext")
    first = asyncio.run(databases.open(first_owner, "notes", SCHEMA))
    other = asyncio.run(databases.open(other_owner, "notes", SCHEMA))
    _insert(first, "kept")

    asyncio.run(databases.release(first_owner))

    assert first.is_closed()
    assert not other.is_closed()
    assert databases.open_databases() == (other,)
    with pytest.raises(ExtensionUnavailableError):
        asyncio.run(databases.open(first_owner, "notes", SCHEMA))
    reloaded = asyncio.run(databases.open(_identity(epoch="epoch-2"), "notes", SCHEMA))
    assert _bodies(reloaded) == ["kept"]


def test_a_second_open_of_the_same_database_is_refused(databases) -> None:
    identity = _identity()
    database = asyncio.run(databases.open(identity, "notes", SCHEMA))

    with pytest.raises(ValueError, match="already open"):
        asyncio.run(databases.open(identity, "notes", SCHEMA))

    assert databases.open_databases() == (database,)


def test_close_closes_every_handle_and_refuses_later_opens(databases) -> None:
    database = asyncio.run(databases.open(_identity(), "notes", SCHEMA))

    databases.close()

    assert database.is_closed()
    assert databases.open_databases() == ()
    with pytest.raises(ExtensionUnavailableError):
        asyncio.run(databases.open(_identity("late_ext"), "notes", SCHEMA))
    asyncio.run(databases.release(_identity()))  # a late release stays harmless


def test_migrations_and_additive_schema_changes_apply_on_reopen(databases) -> None:
    applied: list[str] = []

    def backfill(connection) -> None:
        applied.append("backfill")
        connection.execute("UPDATE notes SET tag='legacy' WHERE tag IS NULL")

    first = asyncio.run(databases.open(_identity(), "notes", SCHEMA))
    _insert(first, "old")
    asyncio.run(databases.release(_identity()))

    evolved = SCHEMA.replace("body TEXT NOT NULL", "body TEXT NOT NULL, tag TEXT")
    reopened = asyncio.run(
        databases.open(
            _identity(epoch="epoch-2"),
            "notes",
            evolved,
            migrations=(Migration("tag_backfill", apply=backfill),),
        )
    )

    with reopened.read() as connection:
        rows = connection.execute("SELECT body, tag FROM notes").fetchall()
    assert [tuple(row) for row in rows] == [("old", "legacy")]
    assert applied == ["backfill"]


@pytest.mark.parametrize(
    "name", ["", "Notes", "../notes", "notes.db", "a/b", "con", "-notes", "x" * 129, 7]
)
def test_invalid_database_names_are_rejected(databases, data_dir, name) -> None:
    with pytest.raises(ValueError, match="Invalid Extension database name"):
        asyncio.run(databases.open(_identity(), name, SCHEMA))

    assert not (data_dir / "extension-data").exists()


def test_an_extension_whose_id_is_not_a_safe_id_cannot_open_databases(databases) -> None:
    with pytest.raises(ValueError, match="cannot open databases"):
        asyncio.run(databases.open(_identity("Notes.Ext"), "notes", SCHEMA))


def test_spec_matches_the_host_opened_database(data_dir) -> None:
    spec = extension_database_spec(data_dir, "swarm", "swarm", SCHEMA)

    assert spec.name == extension_database_name("swarm", "swarm") == "ext.swarm.swarm"
    assert spec.path == data_dir / "extension-data" / "swarm" / "swarm.db"
    with pytest.raises(ValueError, match="schema_sql"):
        extension_database_spec(data_dir, "swarm", "swarm", "  ")
