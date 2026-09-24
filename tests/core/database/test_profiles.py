"""Canonical and disposable profiles, declarations and application identities."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    APPLICATION_IDS,
    CANONICAL,
    DISPOSABLE,
    DatabaseSpec,
    DatabaseUnavailableError,
    open_database,
    read_marker,
)
from core.database.spec import canonical_relative_path
from core.sessions._store_schema import session_database_spec
from tests.core.database.database_test_support import (
    NOTES_SCHEMA_SQL,
    TEST_APPLICATION_ID,
    add_note,
    note_bodies,
    notes_spec,
    projection_spec,
    raw_execute,
)


def _synchronous(database) -> int:
    return int(database.writer.execute("PRAGMA synchronous").fetchone()[0])


def test_canonical_databases_sync_fully_and_disposable_ones_normally(
    data_dir: Path, tmp_path: Path
) -> None:
    canonical = open_database(notes_spec(data_dir))
    disposable = open_database(projection_spec(tmp_path / "index" / "notes-index.db"))
    try:
        assert _synchronous(canonical) == 2
        assert _synchronous(disposable) == 1
        assert int(canonical.writer.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    finally:
        canonical.close()
        disposable.close()


def test_disposable_databases_need_no_marker_and_are_never_registered(
    data_dir: Path, tmp_path: Path
) -> None:
    unmanaged = open_database(projection_spec(tmp_path / "anywhere" / "index.db"))
    unmanaged.close()
    inside = open_database(projection_spec(data_dir / "recall" / "index.db"))
    inside.close()

    marker = read_marker(data_dir)
    assert marker is not None
    assert marker.databases == {}
    assert inside.data_dir is None


def _identity(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as connection:
        return dict(connection.execute("SELECT key, value FROM kernel_meta").fetchall())


def test_a_projection_version_change_discards_and_rebuilds(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    add_note(database, "derived")
    database.close()
    Path(f"{path}-journal").write_bytes(b"")

    rebuilt = open_database(projection_spec(path, projection_version=2))
    try:
        assert note_bodies(rebuilt) == []
    finally:
        rebuilt.close()
    assert _identity(path)["projection_version"] == "2"
    assert rebuilt.database_id != database.database_id


@pytest.mark.parametrize("damage", ["foreign", "newer", "garbage"])
def test_a_foreign_newer_or_damaged_projection_is_discarded(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    add_note(database, "derived")
    database.close()
    if damage == "foreign":
        raw_execute(path, "PRAGMA application_id = 1")
    elif damage == "newer":
        raw_execute(
            path,
            "PRAGMA user_version = 2",
            "UPDATE kernel_meta SET value = '2' WHERE key = 'format_generation'",
        )
    else:
        path.write_bytes(b"X" * 8192)

    rebuilt = open_database(projection_spec(path))
    try:
        assert note_bodies(rebuilt) == []
    finally:
        rebuilt.close()


def test_a_busy_projection_is_unavailable_and_never_discarded(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    add_note(database, "derived")
    database.close()
    raw_execute(path, "PRAGMA journal_mode = DELETE")

    with closing(sqlite3.connect(path, isolation_level=None)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("INSERT INTO notes (body) VALUES ('uncommitted')")
        with pytest.raises(DatabaseUnavailableError):
            open_database(projection_spec(path, projection_version=2))
        holder.execute("ROLLBACK")

    kept = open_database(projection_spec(path))
    try:
        assert note_bodies(kept) == ["derived"]
    finally:
        kept.close()


def test_a_projection_open_in_this_process_is_never_deleted(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    database = open_database(projection_spec(path))
    try:
        add_note(database, "in use")
        with pytest.raises(DatabaseUnavailableError, match="still open"):
            open_database(projection_spec(path, projection_version=2))
        assert note_bodies(database) == ["in use"]
    finally:
        database.close()


def test_spec_validation_rejects_inconsistent_declarations(tmp_path: Path) -> None:
    base: dict[str, Any] = {
        "name": "notes",
        "path": tmp_path / "notes.db",
        "application_id": TEST_APPLICATION_ID,
        "format_generation": 1,
        "schema_sql": NOTES_SCHEMA_SQL,
    }
    with pytest.raises(ValueError, match="projection_version"):
        DatabaseSpec(profile=DISPOSABLE, **base)
    with pytest.raises(ValueError, match="projection_version"):
        DatabaseSpec(profile=CANONICAL, projection_version=1, **base)
    with pytest.raises(ValueError, match="application_id"):
        DatabaseSpec(profile=CANONICAL, **{**base, "application_id": 0})
    with pytest.raises(ValueError, match="format_generation"):
        DatabaseSpec(profile=CANONICAL, **{**base, "format_generation": 0})
    with pytest.raises(ValueError, match="invalid database name"):
        DatabaseSpec(profile=CANONICAL, **{**base, "name": "Notes"})
    with pytest.raises(ValueError, match="unique"):
        from core.database import Migration

        DatabaseSpec(profile=CANONICAL, migrations=(Migration("a"), Migration("a")), **base)


@pytest.mark.parametrize(
    ("name", "relative"),
    [
        ("sessions", "sessions.db"),
        ("provider_usage", "provider-usage.db"),
        ("ext.swarm.board", "extension-data/swarm/board.db"),
    ],
)
def test_canonical_names_map_to_fixed_paths(name: str, relative: str) -> None:
    assert canonical_relative_path(name).as_posix() == relative


def test_application_ids_are_distinct_and_the_session_database_uses_its_own() -> None:
    assert len(set(APPLICATION_IDS.values())) == len(APPLICATION_IDS)
    assert TEST_APPLICATION_ID not in APPLICATION_IDS.values()
    assert APPLICATION_IDS["sessions"] == 0x56425353
    spec = session_database_spec(Path("data") / "sessions.db")
    assert spec.application_id == APPLICATION_IDS["sessions"]
    assert spec.profile == CANONICAL
    assert spec.format_generation == 1
