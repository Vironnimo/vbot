"""Operator status of the data directory."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.database import (
    DatabaseHealth,
    begin_maintenance,
    data_store_status,
    open_database,
)
from tests.core.database.database_test_support import (
    notes_spec,
    snapshot_with_notes,
)


def _degraded(_connection: sqlite3.Connection) -> DatabaseHealth:
    return DatabaseHealth("degraded", "search index is rebuilding", {"indexed": 3})


def test_a_snapshotted_data_directory_is_healthy(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")

    status = data_store_status(data_dir)

    assert status["state"] == "healthy"
    assert status["reason"] is None
    assert status["maintenance"] is None
    assert status["incidents"] == []
    assert status["databases"]["notes"]["state"] == "healthy"
    assert status["databases"]["notes"]["format_generation"] == 1
    assert [item["snapshot_id"] for item in status["snapshots"]] == [snapshot.name]


def test_without_a_verified_snapshot_the_data_directory_is_snapshot_degraded(
    data_dir: Path,
) -> None:
    open_database(notes_spec(data_dir)).close()

    status = data_store_status(data_dir)

    assert status["state"] == "snapshot_degraded"
    assert status["reason"] == "no verified data snapshot is available"


def test_owner_health_is_reported_for_open_and_closed_databases(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "saved")
    spec = notes_spec(data_dir, health=_degraded)

    closed = data_store_status(data_dir, specs=(spec,))
    database = open_database(spec)
    try:
        opened = data_store_status(data_dir, databases=(database,))
    finally:
        database.close()

    for status in (closed, opened):
        assert status["state"] == "degraded"
        assert status["reason"] == "notes: search index is rebuilding"
        assert status["databases"]["notes"]["details"] == {"indexed": 3}


def test_a_missing_or_foreign_registered_database_is_unavailable(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    original = path.read_bytes()
    path.unlink()

    missing = data_store_status(data_dir)
    path.write_bytes(original)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE kernel_meta SET value = ? WHERE key = 'database_id'", ("c" * 32,)
        )
        connection.commit()
    finally:
        connection.close()
    foreign = data_store_status(data_dir)

    assert missing["state"] == "unavailable"
    assert missing["databases"]["notes"]["reason"].startswith(
        "the registered database notes has no file; starting vBot restores it"
    )
    assert foreign["state"] == "unavailable"
    assert foreign["databases"]["notes"]["reason"] == "the database file has another identity"


def test_a_data_directory_without_a_marker_is_unavailable(tmp_path: Path) -> None:
    status = data_store_status(tmp_path)

    assert status["state"] == "unavailable"
    assert status["reason"] == "the data directory has no data-store marker"
    assert status["databases"] == {}


def test_incomplete_maintenance_outranks_every_other_state(data_dir: Path) -> None:
    open_database(notes_spec(data_dir)).close()
    notes_spec(data_dir).path.unlink()
    begin_maintenance(data_dir, "convert")

    status = data_store_status(data_dir)

    assert status["state"] == "maintenance"
    assert status["reason"] == "data maintenance is incomplete (convert)"
    assert status["maintenance"]["operation"] == "convert"


def test_status_never_creates_evolves_or_restores(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged")

    status = data_store_status(data_dir, specs=(notes_spec(data_dir),))

    assert status["state"] == "unavailable"
    assert status["databases"]["notes"]["reason"].startswith("the database file is damaged")
    assert path.read_bytes() == b"damaged"
    assert status["incidents"] == []
