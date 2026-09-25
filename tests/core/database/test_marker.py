"""The data-store marker, the maintenance guard and the operation lock."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    MAINTENANCE_GUARD_FILE_NAME,
    MARKER_FILE_NAME,
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    MarkerEntry,
    begin_maintenance,
    create_data_snapshot,
    finish_maintenance,
    maintenance,
    open_database,
    open_offline_database,
    read_maintenance,
    read_marker,
    unregister_database,
    write_bootstrap_marker,
)
from core.database import marker as marker_module
from core.database.marker import (
    acquire_operation_lock,
    operation_lock_path,
    register_database,
    write_marker_for_databases,
)
from tests.core.database.database_test_support import add_note, notes_spec, stored_bodies


def _marker_payload(data_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((data_dir / MARKER_FILE_NAME).read_text(encoding="utf-8"))
    return payload


def test_bootstrap_marker_lists_no_database(data_dir: Path) -> None:
    assert _marker_payload(data_dir) == {"databases": {}, "format_version": 1}
    marker = read_marker(data_dir)
    assert marker is not None
    assert marker.databases == {}


def test_first_open_creates_and_registers_the_database(data_dir: Path) -> None:
    database = open_database(notes_spec(data_dir))
    database.close()

    assert _marker_payload(data_dir) == {
        "databases": {"notes": {"database_id": database.database_id, "format_generation": 1}},
        "format_version": 1,
    }
    assert database.data_dir == data_dir.resolve()


@pytest.mark.parametrize(
    "payload",
    [
        {"databases": {}},
        {"format_version": 1},
        {"format_version": 2, "databases": {}},
        {"format_version": True, "databases": {}},
        {"format_version": 1, "databases": []},
        {
            "format_version": 1,
            "databases": {"Notes": {"database_id": "a" * 32, "format_generation": 1}},
        },
        {
            "format_version": 1,
            "databases": {"notes": {"database_id": "a" * 31, "format_generation": 1}},
        },
        {
            "format_version": 1,
            "databases": {"notes": {"database_id": "a" * 32, "format_generation": 0}},
        },
        {"format_version": 1, "databases": {"notes": {"database_id": "a" * 32}}},
    ],
    ids=[
        "missing-format",
        "missing-databases",
        "newer-format",
        "boolean-format",
        "database-list",
        "invalid-name",
        "invalid-id",
        "invalid-generation",
        "missing-entry-generation",
    ],
)
def test_a_marker_with_a_missing_invalid_or_newer_known_field_is_refused(
    data_dir: Path, payload: dict
) -> None:
    (data_dir / MARKER_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DatabaseFormatError):
        read_marker(data_dir)
    with pytest.raises(DatabaseFormatError):
        open_database(notes_spec(data_dir))
    assert not notes_spec(data_dir).path.exists()


def test_an_older_vbot_keeps_the_fields_a_newer_one_added_to_the_marker(
    data_dir: Path,
) -> None:
    open_database(notes_spec(data_dir)).close()
    open_database(notes_spec(data_dir, name="ext.demo.state")).close()
    payload = _marker_payload(data_dir)
    payload["retention"] = {"policy": "keep", "limits": [1, 2]}
    payload["databases"]["notes"]["owner"] = {"kind": "core", "since": "2.0"}
    payload["databases"]["ext.demo.state"]["owner"] = "demo"
    (data_dir / MARKER_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")

    marker = read_marker(data_dir)
    assert marker is not None
    assert marker.databases["notes"] == MarkerEntry(payload["databases"]["notes"]["database_id"], 1)
    open_database(notes_spec(data_dir)).close()
    # Registering and releasing a database rewrite the marker.
    open_database(notes_spec(data_dir, name="tasks")).close()
    unregister_database(data_dir, "ext.demo.state")

    rewritten = _marker_payload(data_dir)
    assert rewritten["retention"] == {"policy": "keep", "limits": [1, 2]}
    assert rewritten["databases"]["notes"] == payload["databases"]["notes"]
    assert set(rewritten["databases"]) == {"notes", "tasks"}
    assert set(rewritten["databases"]["tasks"]) == {"database_id", "format_generation"}


def test_existing_root_without_marker_never_authorizes_a_database(tmp_path: Path) -> None:
    root = tmp_path / "uninitialized"
    root.mkdir()

    with pytest.raises(DatabaseFormatError, match="does not authorize"):
        open_database(notes_spec(root))
    assert not notes_spec(root).path.exists()


def test_a_canonical_database_must_sit_at_its_canonical_path(data_dir: Path) -> None:
    moved = replace(notes_spec(data_dir), path=data_dir / "notes-copy.db")

    with pytest.raises(DatabaseFormatError, match="canonical path"):
        open_database(moved)


def test_a_registered_database_that_is_missing_without_a_snapshot_is_unavailable(
    data_dir: Path,
) -> None:
    open_database(notes_spec(data_dir)).close()
    notes_spec(data_dir).path.unlink()

    with pytest.raises(DatabaseUnavailableError, match="missing"):
        open_database(notes_spec(data_dir))
    assert not notes_spec(data_dir).path.exists()


def test_an_unlisted_valid_database_is_adopted_after_verification(data_dir: Path) -> None:
    database = open_offline_database(notes_spec(data_dir))
    add_note(database, "prepared offline")
    database.close()

    assert stored_bodies(notes_spec(data_dir)) == ["prepared offline"]
    marker = read_marker(data_dir)
    assert marker is not None
    assert marker.databases["notes"].database_id == database.database_id


def test_an_unlisted_foreign_file_is_never_registered_or_replaced(data_dir: Path) -> None:
    path = notes_spec(data_dir).path
    path.write_bytes(b"foreign bytes that are not a database")

    with pytest.raises(DatabaseCorruptError):
        open_database(notes_spec(data_dir))
    assert path.read_bytes() == b"foreign bytes that are not a database"
    marker = read_marker(data_dir)
    assert marker is not None
    assert marker.databases == {}


def test_registration_refuses_another_identity_for_a_listed_name(data_dir: Path) -> None:
    database = open_database(notes_spec(data_dir))
    database.close()

    register_database(data_dir, "notes", MarkerEntry(database.database_id, 1))
    with pytest.raises(DatabaseFormatError, match="different identity"):
        register_database(data_dir, "notes", MarkerEntry("b" * 32, 1))


def test_marker_for_databases_requires_canonical_paths(data_dir: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    database = open_offline_database(notes_spec(elsewhere))
    database.close()

    with pytest.raises(DatabaseFormatError, match="canonical path"):
        write_marker_for_databases(data_dir, [database.path])
    assert _marker_payload(data_dir)["databases"] == {}


def test_the_maintenance_guard_blocks_opening_and_snapshots_until_finished(
    data_dir: Path,
) -> None:
    open_database(notes_spec(data_dir)).close()
    record = begin_maintenance(data_dir, "convert")

    assert read_maintenance(data_dir) == record
    with pytest.raises(DatabaseFormatError, match="maintenance is incomplete"):
        open_database(notes_spec(data_dir))
    with pytest.raises(DatabaseFormatError, match="maintenance is incomplete"):
        create_data_snapshot(data_dir, reason="test")
    with pytest.raises(DatabaseFormatError, match="already incomplete"):
        begin_maintenance(data_dir, "restore")

    finish_maintenance(data_dir, record)
    assert read_maintenance(data_dir) is None
    open_database(notes_spec(data_dir)).close()


def test_only_the_same_operation_resumes_an_interrupted_guard(data_dir: Path) -> None:
    first = begin_maintenance(data_dir, "restore")

    with pytest.raises(DatabaseFormatError):
        begin_maintenance(data_dir, "convert", resume=True)
    resumed = begin_maintenance(data_dir, "restore", resume=True)

    assert resumed.operation_id != first.operation_id
    with pytest.raises(DatabaseFormatError, match="another operation"):
        finish_maintenance(data_dir, first)
    finish_maintenance(data_dir, resumed)
    assert read_maintenance(data_dir) is None


def test_the_maintenance_block_keeps_the_guard_when_the_operation_fails(data_dir: Path) -> None:
    with maintenance(data_dir, "convert"):
        assert read_maintenance(data_dir) is not None
    assert read_maintenance(data_dir) is None

    with pytest.raises(RuntimeError), maintenance(data_dir, "convert"):
        raise RuntimeError("interrupted conversion")
    guard = read_maintenance(data_dir)
    assert guard is not None
    assert guard.operation == "convert"


def test_a_malformed_guard_still_blocks(data_dir: Path) -> None:
    (data_dir / MAINTENANCE_GUARD_FILE_NAME).write_text("{}", encoding="utf-8")

    guard = read_maintenance(data_dir)
    assert guard is not None
    assert guard.operation == "unknown"
    with pytest.raises(DatabaseFormatError, match="maintenance is incomplete"):
        open_database(notes_spec(data_dir))


def test_the_operation_lock_is_exclusive_and_never_broken_by_age(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = acquire_operation_lock(data_dir)
    assert owner is not None
    old = operation_lock_path(data_dir).stat().st_mtime - 120
    os.utime(operation_lock_path(data_dir), (old, old))
    monkeypatch.setattr(marker_module, "OPERATION_LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        assert acquire_operation_lock(data_dir, timeout=0.05) is None
        with pytest.raises(DatabaseUnavailableError, match="busy"):
            write_bootstrap_marker_under_lock(data_dir)
    finally:
        owner.release()

    again = acquire_operation_lock(data_dir, timeout=0.05)
    assert again is not None
    again.release()


def write_bootstrap_marker_under_lock(data_dir: Path) -> None:
    register_database(data_dir, "notes", MarkerEntry("a" * 32, 1))


def test_bootstrap_marker_authorizes_a_fresh_root(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    write_bootstrap_marker(root)

    database = open_database(notes_spec(root))
    database.close()
    assert set(_marker_payload(root)["databases"]) == {"notes"}
