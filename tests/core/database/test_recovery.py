"""Quarantine, recovery incidents, automatic restore and per-member operator restore."""

from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseUnavailableError,
    IncidentConflictError,
    MarkerEntry,
    acknowledge_incident,
    active_incidents,
    data_store_status,
    has_live_connection,
    open_database,
    read_incident,
    read_maintenance,
    read_marker,
    restore_data_snapshot,
)
from core.database import marker as marker_module
from core.database import recovery as recovery_module
from core.database.marker import _write_marker, acquire_operation_lock
from core.database.recovery import (
    QUARANTINE_ROOT_NAME,
    auto_restore_if_needed,
    incident_path,
    quarantine_database,
    quarantine_root,
    write_incident,
)
from core.database.snapshots import SNAPSHOT_MANIFEST_NAME
from tests.core.database.database_test_support import (
    NOTES_SCHEMA_SQL,
    add_note,
    note_bodies,
    notes_spec,
    raw_execute,
    snapshot_with_notes,
    stored_bodies,
)


def _database_id(data_dir: Path, name: str = "notes") -> str:
    marker = read_marker(data_dir)
    assert marker is not None
    return marker.databases[name].database_id


def _restore(data_dir: Path, spec=None) -> bool:
    spec = spec or notes_spec(data_dir)
    return auto_restore_if_needed(data_dir, spec, _database_id(data_dir, spec.name))


def _quarantine_batches(data_dir: Path, name: str = "notes") -> list[Path]:
    root = quarantine_root(data_dir) / name
    return sorted(root.iterdir()) if root.exists() else []


def _into_quarantine(destination: str | Path) -> bool:
    return QUARANTINE_ROOT_NAME in Path(destination).parts


def test_a_damaged_database_is_restored_on_open_with_incident_and_quarantine(
    data_dir: Path,
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged")

    database = open_database(notes_spec(data_dir))
    try:
        assert note_bodies(database) == ["saved"]
        status = data_store_status(data_dir, databases=(database,))
    finally:
        database.close()

    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["verification"] == "ok"
    assert incident["database"] == "notes"
    assert incident["acknowledged"] is False
    assert incident["restored_snapshot_id"] == snapshot.name
    assert incident["cause"] == "malformed canonical notes database"
    quarantine = Path(incident["quarantine"])
    assert quarantine.parent == quarantine_root(data_dir) / "notes"
    assert re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", quarantine.name)
    assert (quarantine / "notes.db").read_bytes() == b"damaged"
    assert [item["incident_id"] for item in active_incidents(data_dir)] == [incident["incident_id"]]
    assert status["state"] == "recovered_with_incident"


def test_a_missing_registered_database_is_restored_without_quarantine(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "saved")
    notes_spec(data_dir).path.unlink()

    assert stored_bodies(notes_spec(data_dir)) == ["saved"]
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["cause"] == "missing canonical notes database"
    assert incident["quarantine"] is None


def test_identity_mismatch_recovers_and_leaves_no_connection_open(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    raw_execute(path, f"UPDATE kernel_meta SET value = '{'b' * 32}' WHERE key = 'database_id'")

    assert stored_bodies(notes_spec(data_dir)) == ["saved"]
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["cause"] == "notes database identity mismatch"
    assert not has_live_connection(path)


def test_schema_shape_corruption_recovers_from_a_compatible_snapshot(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "saved")
    raw_execute(
        notes_spec(data_dir).path,
        "PRAGMA writable_schema = ON",
        "UPDATE sqlite_master SET sql = replace(sql, 'length(tag) <= 40', 'length(tag) <= 39') "
        "WHERE name = 'notes'",
    )

    assert stored_bodies(notes_spec(data_dir)) == ["saved"]
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["cause"] == "notes database table schema mismatch"


@pytest.mark.parametrize("mode", ["manual", "automatic"])
def test_restore_rejects_an_incompatible_member_before_touching_data(
    data_dir: Path, mode: str
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    # This vBot changed the table contract without a new name or generation.
    changed = notes_spec(
        data_dir, schema_sql=NOTES_SCHEMA_SQL.replace("length(tag) <= 40", "length(tag) <= 41")
    )
    path = changed.path
    if mode == "automatic":
        path.write_bytes(b"damaged")
    original = path.read_bytes()

    if mode == "manual":
        with pytest.raises(DatabaseFormatError, match="cannot be opened"):
            restore_data_snapshot(data_dir, snapshot, specs=(changed,))
        assert read_maintenance(data_dir) is None
    else:
        assert _restore(data_dir, changed) is False
    assert path.read_bytes() == original
    assert not quarantine_root(data_dir).exists()
    assert read_incident(data_dir, "notes") is None


def test_a_newer_vbot_database_is_never_replaced(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    raw_execute(
        path,
        "INSERT INTO kernel_migrations (name, applied_at, applied_by_version, breaks_older) "
        "VALUES ('notes.future', '2027-01-01T00:00:00Z', '9.0.0', 1)",
    )
    original = path.read_bytes()

    assert _restore(data_dir) is False
    assert path.read_bytes() == original
    assert not quarantine_root(data_dir).exists()


def test_failed_quarantine_keeps_the_original_and_the_guard_until_repeated(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    database = open_database(notes_spec(data_dir))
    add_note(database, "newer")
    database.close()
    path = notes_spec(data_dir).path
    original = path.read_bytes()
    real_replace = recovery_module.os.replace

    def fail_quarantine(source: str | Path, destination: str | Path) -> None:
        if _into_quarantine(destination):
            raise OSError("injected quarantine failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_quarantine)
    with pytest.raises(DatabaseUnavailableError):
        restore_data_snapshot(data_dir, snapshot)
    assert path.read_bytes() == original
    guard = read_maintenance(data_dir)
    assert guard is not None
    assert guard.operation == "restore"
    with pytest.raises(DatabaseFormatError, match="maintenance is incomplete"):
        open_database(notes_spec(data_dir))

    monkeypatch.setattr(recovery_module.os, "replace", real_replace)
    assert restore_data_snapshot(data_dir, snapshot) == ["notes"]
    assert read_maintenance(data_dir) is None
    assert stored_bodies(notes_spec(data_dir)) == ["saved"]


def test_incident_publication_failure_does_not_report_recovery(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged")
    real_replace = recovery_module.os.replace

    def fail_incident(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == incident_path(data_dir, "notes"):
            raise OSError("injected incident publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_incident)

    with pytest.raises(DatabaseCorruptError):
        open_database(notes_spec(data_dir))
    assert path.read_bytes() == b"damaged"
    assert _quarantine_batches(data_dir) == []


def test_final_incident_failure_leaves_pending_evidence_and_completes_on_next_open(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_with_notes(data_dir, "saved")
    notes_spec(data_dir).path.write_bytes(b"damaged")
    real_replace = recovery_module.os.replace
    incident_replaces = 0

    def fail_final_incident(source: str | Path, destination: str | Path) -> None:
        nonlocal incident_replaces
        if Path(destination) == incident_path(data_dir, "notes"):
            incident_replaces += 1
            if incident_replaces == 2:
                raise OSError("injected final incident publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_final_incident)
    with pytest.raises(DatabaseCorruptError):
        open_database(notes_spec(data_dir))
    pending = read_incident(data_dir, "notes")
    assert pending is not None
    assert pending["verification"] == "pending"
    assert pending["recovered_at"] is None
    assert (Path(pending["quarantine"]) / "notes.db").read_bytes() == b"damaged"

    monkeypatch.setattr(recovery_module.os, "replace", real_replace)
    assert stored_bodies(notes_spec(data_dir)) == ["saved"]
    completed = read_incident(data_dir, "notes")
    assert completed is not None
    assert completed["incident_id"] == pending["incident_id"]
    assert completed["verification"] == "ok"
    assert completed["quarantine"] == pending["quarantine"]
    assert completed["possible_loss_interval"] == pending["possible_loss_interval"]


def test_final_incident_failure_never_restores_an_older_snapshot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_with_notes(data_dir, "one")
    snapshot_with_notes(data_dir, "two")
    notes_spec(data_dir).path.write_bytes(b"damaged")
    real_publish = recovery_module.write_incident

    def fail_final_incident(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("verification") == "ok":
            raise DatabaseUnavailableError("injected finalization failure")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(recovery_module, "write_incident", fail_final_incident)
    assert _restore(data_dir) is False
    monkeypatch.setattr(recovery_module, "write_incident", real_publish)

    assert stored_bodies(notes_spec(data_dir)) == ["one", "two"]
    assert len(_quarantine_batches(data_dir)) == 1


def test_concurrent_recovery_reprobes_after_the_first_owner_finishes(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "saved")
    notes_spec(data_dir).path.write_bytes(b"damaged")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def recover() -> None:
        barrier.wait()
        results.append(_restore(data_dir))

    workers = [threading.Thread(target=recover) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(results) == [False, True]
    assert len(_quarantine_batches(data_dir)) == 1


@pytest.mark.parametrize("rejection", ["hash", "live"])
def test_a_rejected_restore_preserves_the_existing_incident(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, rejection: str
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    write_incident(
        data_dir,
        "notes",
        cause="earlier recovery",
        quarantine_path=None,
        restored_snapshot_id="earlier",
        restored_snapshot_time="2026-08-31T10:00:00Z",
    )
    evidence = incident_path(data_dir, "notes").read_bytes()
    path = notes_spec(data_dir).path
    original = path.read_bytes()
    if rejection == "hash":
        manifest_path = snapshot / SNAPSHOT_MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["members"]["notes"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        expected: type[Exception] = DatabaseCorruptError
    else:
        monkeypatch.setattr(recovery_module, "has_live_connection", lambda _path: True)
        expected = DatabaseUnavailableError

    with pytest.raises(expected):
        restore_data_snapshot(data_dir, snapshot)
    assert path.read_bytes() == original
    assert incident_path(data_dir, "notes").read_bytes() == evidence


def test_a_copy_failure_stops_the_candidate_fallback(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_with_notes(data_dir, "one")
    newest = snapshot_with_notes(data_dir, "two")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged")
    attempted: list[Path] = []
    real_copy = shutil.copy2

    def fail_first_copy(source: Any, destination: Any, *args: Any, **kwargs: Any) -> Any:
        attempted.append(Path(source))
        if len(attempted) == 1:
            raise OSError("injected temporary write failure")
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(recovery_module.shutil, "copy2", fail_first_copy)

    assert _restore(data_dir) is False
    assert attempted == [newest / "notes.db"]
    assert path.read_bytes() == b"damaged"
    assert not quarantine_root(data_dir).exists()
    assert read_incident(data_dir, "notes") is None


@pytest.mark.parametrize(
    "serialized", [b"{", b"\xff", b"[]", b'{"incident_id":"a","acknowledged":false}']
)
def test_open_preserves_invalid_incident_evidence(data_dir: Path, serialized: bytes) -> None:
    snapshot_with_notes(data_dir, "saved")
    evidence = incident_path(data_dir, "notes")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_bytes(serialized)

    with pytest.raises(DatabaseCorruptError):
        open_database(notes_spec(data_dir))
    assert evidence.read_bytes() == serialized


def test_a_pending_restore_never_mistakes_the_untouched_original_for_success(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    database = open_database(notes_spec(data_dir))
    add_note(database, "newer original")
    database.close()
    path = notes_spec(data_dir).path
    original = path.read_bytes()
    real_replace = recovery_module.os.replace

    def fail_quarantine(source: str | Path, destination: str | Path) -> None:
        if _into_quarantine(destination):
            raise PermissionError("injected quarantine access failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_quarantine)
    with pytest.raises(DatabaseUnavailableError):
        restore_data_snapshot(data_dir, snapshot)
    pending = read_incident(data_dir, "notes")
    assert pending is not None
    assert pending["verification"] == "pending"
    monkeypatch.setattr(recovery_module.os, "replace", real_replace)

    assert _restore(data_dir) is False
    assert path.read_bytes() == original
    assert read_incident(data_dir, "notes") == pending


@pytest.mark.parametrize("partial_install", [False, True])
def test_a_restore_retry_keeps_the_original_quarantine_and_failure_time(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, partial_install: bool
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged original")
    real_replace = recovery_module.os.replace

    def fail_install(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == path:
            raise PermissionError("injected restore publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_install)
    with pytest.raises(DatabaseUnavailableError):
        restore_data_snapshot(data_dir, snapshot)
    pending = read_incident(data_dir, "notes")
    assert pending is not None
    assert not path.exists()
    if partial_install:
        path.write_bytes(b"damaged retry output")
    monkeypatch.setattr(recovery_module.os, "replace", real_replace)

    assert _restore(data_dir) is True
    completed = read_incident(data_dir, "notes")
    assert completed is not None
    assert completed["incident_id"] == pending["incident_id"]
    assert completed["cause"] == "manual operator restore"
    assert completed["possible_loss_interval"] == pending["possible_loss_interval"]
    assert completed["quarantine"] == pending["quarantine"]
    assert (Path(completed["quarantine"]) / "notes.db").read_bytes() == b"damaged original"


def test_a_retry_after_quarantine_rollback_records_the_actual_evidence(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged original")
    real_replace = recovery_module.os.replace

    def fail_quarantine(source: str | Path, destination: str | Path) -> None:
        if _into_quarantine(destination):
            raise PermissionError("injected quarantine failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_quarantine)
    with pytest.raises(DatabaseUnavailableError):
        restore_data_snapshot(data_dir, snapshot)
    pending = read_incident(data_dir, "notes")
    assert pending is not None
    assert not Path(pending["quarantine"]).exists()
    monkeypatch.setattr(recovery_module.os, "replace", real_replace)

    assert _restore(data_dir) is True
    completed = read_incident(data_dir, "notes")
    assert completed is not None
    assert (Path(completed["quarantine"]) / "notes.db").read_bytes() == b"damaged original"


def test_a_failed_quarantine_rollback_keeps_the_only_copy(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = notes_spec(data_dir).path
    sidecar = Path(f"{path}-wal")
    path.write_bytes(b"original database")
    sidecar.write_bytes(b"original wal")
    real_replace = recovery_module.os.replace

    def fail_sidecar_and_rollback(source: str | Path, destination: str | Path) -> None:
        if Path(source) == sidecar or Path(destination) == path:
            raise OSError("injected move failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_sidecar_and_rollback)
    result = quarantine_database(data_dir, "notes")

    assert result.status == "failed"
    assert result.path is not None
    assert (result.path / path.name).read_bytes() == b"original database"
    assert sidecar.read_bytes() == b"original wal"


def test_a_quarantine_durability_failure_rolls_back_the_whole_bundle(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = notes_spec(data_dir).path
    sidecar = Path(f"{path}-wal")
    path.write_bytes(b"database evidence")
    sidecar.write_bytes(b"wal evidence")
    real_fsync = recovery_module.fsync_dir

    def fail_quarantine_sync(directory: Path) -> None:
        if directory.parent == quarantine_root(data_dir) / "notes":
            raise OSError("injected quarantine durability failure")
        real_fsync(directory)

    monkeypatch.setattr(recovery_module, "fsync_dir", fail_quarantine_sync)
    result = quarantine_database(data_dir, "notes")

    assert result.status == "failed"
    assert result.path is None
    assert path.read_bytes() == b"database evidence"
    assert sidecar.read_bytes() == b"wal evidence"
    assert _quarantine_batches(data_dir) == []


def test_operator_restore_replaces_only_the_selected_members(data_dir: Path) -> None:
    notes = open_database(notes_spec(data_dir))
    tasks = open_database(notes_spec(data_dir, name="tasks"))
    try:
        add_note(notes, "saved note")
        add_note(tasks, "saved task")
        from core.database import create_data_snapshot

        snapshot = create_data_snapshot(data_dir, reason="test", databases=(notes, tasks))
        assert snapshot is not None
        add_note(notes, "later note")
        add_note(tasks, "later task")
    finally:
        notes.close()
        tasks.close()

    assert restore_data_snapshot(data_dir, snapshot, names=["notes"]) == ["notes"]

    assert stored_bodies(notes_spec(data_dir)) == ["saved note"]
    assert stored_bodies(notes_spec(data_dir, name="tasks")) == ["saved task", "later task"]
    assert read_incident(data_dir, "tasks") is None
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["cause"] == "manual operator restore"
    assert read_maintenance(data_dir) is None


def test_operator_restore_checks_every_selected_member_first(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    database = open_database(notes_spec(data_dir))
    add_note(database, "later")
    database.close()
    path = notes_spec(data_dir).path
    original = path.read_bytes()

    assert restore_data_snapshot(data_dir, snapshot, check_only=True) == ["notes"]
    with pytest.raises(DatabaseFormatError, match="has no tasks member"):
        restore_data_snapshot(data_dir, snapshot, names=["notes", "tasks"])
    _write_marker(data_dir, {"notes": MarkerEntry("f" * 32, 1)})
    with pytest.raises(DatabaseFormatError, match="does not belong"):
        restore_data_snapshot(data_dir, snapshot)

    assert path.read_bytes() == original
    assert read_maintenance(data_dir) is None
    assert read_incident(data_dir, "notes") is None


def test_operator_restore_requires_the_current_marker(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    (data_dir / marker_module.MARKER_FILE_NAME).unlink()

    with pytest.raises(DatabaseFormatError, match="does not authorize"):
        restore_data_snapshot(data_dir, snapshot)


def _recovered(data_dir: Path) -> dict[str, Any]:
    snapshot_with_notes(data_dir, "saved")
    notes_spec(data_dir).path.write_bytes(b"damaged")
    open_database(notes_spec(data_dir)).close()
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    return incident


def test_acknowledging_an_incident_is_durable_and_idempotent(data_dir: Path) -> None:
    incident = _recovered(data_dir)

    assert acknowledge_incident(data_dir, incident["incident_id"]) is True
    assert acknowledge_incident(data_dir, incident["incident_id"]) is True

    stored = read_incident(data_dir, "notes")
    assert stored == {**incident, "acknowledged": True}
    assert active_incidents(data_dir) == []
    assert data_store_status(data_dir)["state"] == "healthy"


def test_acknowledging_without_any_incident_reports_nothing(data_dir: Path) -> None:
    assert acknowledge_incident(data_dir, "a" * 32) is False


def test_acknowledging_a_stale_incident_id_conflicts(data_dir: Path) -> None:
    incident = _recovered(data_dir)
    evidence = incident_path(data_dir, "notes").read_bytes()

    with pytest.raises(IncidentConflictError):
        acknowledge_incident(data_dir, "0" * 32)
    assert incident_path(data_dir, "notes").read_bytes() == evidence
    assert read_incident(data_dir, "notes") == incident


def test_acknowledging_never_rewrites_invalid_evidence(data_dir: Path) -> None:
    evidence = incident_path(data_dir, "notes")
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"[]")

    with pytest.raises(DatabaseCorruptError):
        acknowledge_incident(data_dir, "a" * 32)
    assert evidence.read_bytes() == b"[]"


def test_an_unreadable_incident_is_unavailable(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = _recovered(data_dir)
    target = incident_path(data_dir, "notes")
    real_read_text = Path.read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == target:
            raise PermissionError("injected inaccessible incident")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(DatabaseUnavailableError):
        acknowledge_incident(data_dir, incident["incident_id"])


def test_acknowledging_waits_for_the_operation_lock(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = _recovered(data_dir)
    owner = acquire_operation_lock(data_dir)
    assert owner is not None
    monkeypatch.setattr(marker_module, "OPERATION_LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(DatabaseUnavailableError, match="busy"):
            acknowledge_incident(data_dir, incident["incident_id"])
    finally:
        owner.release()
    assert read_incident(data_dir, "notes") == incident


def test_acknowledgement_never_acknowledges_a_newer_incident_written_meanwhile(
    data_dir: Path,
) -> None:
    observed = _recovered(data_dir)
    owner = acquire_operation_lock(data_dir)
    assert owner is not None
    outcome: list[BaseException | bool] = []

    def acknowledge() -> None:
        try:
            outcome.append(acknowledge_incident(data_dir, observed["incident_id"]))
        except BaseException as exc:  # noqa: BLE001 - the test inspects the outcome
            outcome.append(exc)

    acknowledger = threading.Thread(target=acknowledge, name="incident-acknowledger")
    try:
        acknowledger.start()
        # A recovery holding the lock replaces the incident the caller observed.
        write_incident(
            data_dir,
            "notes",
            cause="a newer failure",
            quarantine_path=None,
            restored_snapshot_id=observed["restored_snapshot_id"],
            restored_snapshot_time=observed["restored_snapshot_time"],
        )
    finally:
        owner.release()
    acknowledger.join()

    assert len(outcome) == 1
    assert isinstance(outcome[0], IncidentConflictError)
    newer = read_incident(data_dir, "notes")
    assert newer is not None
    assert newer["cause"] == "a newer failure"
    assert newer["acknowledged"] is False
