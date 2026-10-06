"""Quarantine, recovery incidents, automatic restore and per-member operator restore."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.database import (
    MARKER_FILE_NAME,
    DatabaseCorruptError,
    DatabaseFormatError,
    DatabaseSchemaMismatchError,
    DatabaseSpec,
    DatabaseUnavailableError,
    IncidentConflictError,
    acknowledge_incident,
    active_incidents,
    create_data_snapshot,
    data_store_status,
    has_live_connection,
    maintenance,
    open_database,
    read_incident,
    read_maintenance,
    read_marker,
    restore_data_snapshot,
    retire_core_databases,
    snapshot_root,
    unregister_database,
)
from core.database import recovery as recovery_module
from core.database.marker import acquire_operation_lock
from core.database.recovery import (
    QUARANTINE_ROOT_NAME,
    auto_restore_if_needed,
    incident_path,
    quarantine_root,
    write_incident,
)
from tests.core.database.database_test_support import (
    NOTES_SCHEMA_SQL,
    add_note,
    note_bodies,
    notes_spec,
    raw_execute,
    rewrite_manifest,
    rewrite_marker,
    snapshot_with_notes,
    stored_bodies,
)


def _database_id(data_dir: Path, name: str = "notes") -> str:
    marker = read_marker(data_dir)
    assert marker is not None
    return marker.databases[name].database_id


def _restore(data_dir: Path, spec: DatabaseSpec | None = None) -> bool:
    """The locked re-probe and restore an open runs for a damaged or pending database."""
    spec = spec or notes_spec(data_dir)
    return auto_restore_if_needed(data_dir, spec, _database_id(data_dir, spec.name))


def _quarantine_batches(data_dir: Path, name: str = "notes") -> list[Path]:
    root = quarantine_root(data_dir) / name
    return sorted(root.iterdir()) if root.exists() else []


def _into_quarantine(destination: str | Path) -> bool:
    return QUARANTINE_ROOT_NAME in Path(destination).parts


def _failing_replace(
    fails: Callable[[Path, Path], bool], error: type[OSError] = OSError
) -> Callable[[str | Path, str | Path], None]:
    """``os.replace`` that raises ``error`` for the moves ``fails`` selects."""
    real_replace = os.replace

    def replace(source: str | Path, destination: str | Path) -> None:
        if fails(Path(source), Path(destination)):
            raise error("injected move failure")
        real_replace(source, destination)

    return replace


def _garbage(path: Path) -> None:
    path.write_bytes(b"damaged")


def _missing(path: Path) -> None:
    path.unlink()


def _foreign_identity(path: Path) -> None:
    raw_execute(path, f"UPDATE kernel_meta SET value = '{'b' * 32}' WHERE key = 'database_id'")


def _corrupt_pages(path: Path) -> None:
    """Overwrite the table and index root pages inside an otherwise valid file."""
    connection = sqlite3.connect(path)
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        # The open probe may read either.
        root_pages = [
            int(row[0])
            for row in connection.execute(
                "SELECT rootpage FROM sqlite_master WHERE tbl_name = 'notes' AND rootpage > 0"
            )
        ]
    finally:
        connection.close()
    with path.open("r+b") as handle:
        for root_page in root_pages:
            handle.seek((root_page - 1) * page_size)
            handle.write(b"\xff" * page_size)


@pytest.mark.parametrize(
    ("damage", "cause"),
    [
        (_garbage, "malformed canonical notes database"),
        (_missing, "missing canonical notes database"),
        (_foreign_identity, "notes database identity mismatch"),
        # Which damaged page the probe reads first decides the cause.
        (_corrupt_pages, None),
    ],
    ids=["garbage", "missing", "identity", "page-corruption"],
)
def test_a_damaged_database_is_restored_on_open_with_an_incident(
    data_dir: Path,
    caplog: pytest.LogCaptureFixture,
    damage: Callable[[Path], None],
    cause: str | None,
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    damage(path)
    damaged = path.read_bytes() if path.exists() else None

    with caplog.at_level(logging.WARNING, logger="vbot.database"):
        database = open_database(notes_spec(data_dir))
    try:
        assert note_bodies(database) == ["saved"]
        status = data_store_status(data_dir, databases=(database,))
    finally:
        database.close()

    assert not has_live_connection(path)
    assert status["state"] == "recovered_with_incident"
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["verification"] == "ok"
    assert incident["database"] == "notes"
    assert incident["acknowledged"] is False
    assert incident["restored_snapshot_id"] == snapshot.name
    if cause is not None:
        assert incident["cause"] == cause
    assert [item["incident_id"] for item in active_incidents(data_dir)] == [incident["incident_id"]]
    # Data written after the snapshot may be lost: the operator reads it at ERROR.
    [report] = [record for record in caplog.records if record.name == "vbot.database"]
    assert report.levelno == logging.ERROR
    assert snapshot.name in report.getMessage()
    if damaged is None:
        assert incident["quarantine"] is None
        assert not quarantine_root(data_dir).exists()
    else:
        quarantine = Path(incident["quarantine"])
        assert _quarantine_batches(data_dir) == [quarantine]
        assert re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", quarantine.name)
        assert (quarantine / "notes.db").read_bytes() == damaged
        assert str(quarantine) in report.getMessage()


@pytest.mark.parametrize("changed", ["live-table", "declaration"])
def test_a_schema_mismatch_refuses_without_quarantine_or_restore(
    data_dir: Path, changed: str
) -> None:
    snapshot_with_notes(data_dir, "saved")
    database = open_database(notes_spec(data_dir))
    add_note(database, "newer than the snapshot")
    database.close()
    path = notes_spec(data_dir).path
    spec = notes_spec(data_dir)
    if changed == "live-table":
        raw_execute(
            path,
            "PRAGMA writable_schema = ON",
            "UPDATE sqlite_master SET sql = replace(sql, 'length(tag) <= 40', "
            "'length(tag) <= 39') WHERE name = 'notes'",
        )
    else:
        # This vBot changed the table contract without a new name or generation.
        spec = notes_spec(
            data_dir, schema_sql=NOTES_SCHEMA_SQL.replace("length(tag) <= 40", "length(tag) <= 41")
        )
    original = path.read_bytes()

    with pytest.raises(DatabaseSchemaMismatchError) as caught:
        open_database(spec)

    assert isinstance(caught.value, DatabaseFormatError)
    assert not isinstance(caught.value, DatabaseCorruptError)
    assert caught.value.database == "notes"
    assert caught.value.object_name == "column notes.tag"
    assert "declared shape" in caught.value.difference
    assert "only additive changes are allowed" in str(caught.value)
    # The locked re-probe of an automatic restore refuses the intact file too.
    assert _restore(data_dir, spec) is False
    assert path.read_bytes() == original
    assert not quarantine_root(data_dir).exists()
    assert read_incident(data_dir, "notes") is None


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
        with pytest.raises(DatabaseCorruptError):
            open_database(changed)
    assert path.read_bytes() == original
    assert not quarantine_root(data_dir).exists()
    assert read_incident(data_dir, "notes") is None


@pytest.mark.parametrize("condition", ["newer-vbot", "unreadable"])
def test_a_newer_or_unreadable_database_is_never_replaced(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    deny_access: Callable[[Path], None],
    condition: str,
) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    if condition == "newer-vbot":
        raw_execute(
            path,
            "INSERT INTO kernel_migrations (name, applied_at, applied_by_version, breaks_older) "
            "VALUES ('notes.future', '2027-01-01T00:00:00Z', '9.0.0', 1)",
        )
    original = path.read_bytes()
    if condition == "unreadable":
        # A file that cannot even be checked may hold data newer than any snapshot.
        deny_access(path)
        with pytest.raises(DatabaseUnavailableError, match="unavailable"):
            open_database(notes_spec(data_dir))

    assert _restore(data_dir) is False
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert not quarantine_root(data_dir).exists()


def test_a_failed_restore_keeps_the_original_the_guard_and_its_pending_evidence(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    database = open_database(notes_spec(data_dir))
    add_note(database, "newer original")
    database.close()
    path = notes_spec(data_dir).path
    original = path.read_bytes()

    with monkeypatch.context() as patched:
        patched.setattr(
            os,
            "replace",
            _failing_replace(lambda _source, target: _into_quarantine(target), PermissionError),
        )
        with pytest.raises(DatabaseUnavailableError):
            restore_data_snapshot(data_dir, snapshot)
    assert path.read_bytes() == original
    pending = read_incident(data_dir, "notes")
    assert pending is not None
    assert pending["verification"] == "pending"
    guard = read_maintenance(data_dir)
    assert guard is not None
    assert guard.operation == "restore"
    with pytest.raises(DatabaseFormatError, match="maintenance is incomplete"):
        open_database(notes_spec(data_dir))
    # The re-probe never mistakes the untouched original for the restored copy.
    assert _restore(data_dir) is False
    assert path.read_bytes() == original
    assert read_incident(data_dir, "notes") == pending

    assert restore_data_snapshot(data_dir, snapshot).databases == ("notes",)
    assert read_maintenance(data_dir) is None
    assert stored_bodies(notes_spec(data_dir)) == ["saved"]


def test_incident_publication_failure_does_not_report_recovery(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged")
    evidence = incident_path(data_dir, "notes")
    monkeypatch.setattr(os, "replace", _failing_replace(lambda _source, target: target == evidence))

    with pytest.raises(DatabaseCorruptError):
        open_database(notes_spec(data_dir))
    assert path.read_bytes() == b"damaged"
    assert _quarantine_batches(data_dir) == []


def test_a_final_incident_failure_stops_at_pending_evidence_that_the_next_open_completes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    snapshot_with_notes(data_dir, "one")
    snapshot_with_notes(data_dir, "two")
    notes_spec(data_dir).path.write_bytes(b"damaged")
    evidence = incident_path(data_dir, "notes")
    publications: list[Path] = []

    def final_publication(_source: Path, target: Path) -> bool:
        if target == evidence:
            publications.append(target)
        return len(publications) == 2 and target == evidence

    with monkeypatch.context() as patched, caplog.at_level(logging.ERROR, logger="vbot.database"):
        patched.setattr(os, "replace", _failing_replace(final_publication))
        with pytest.raises(DatabaseCorruptError):
            open_database(notes_spec(data_dir))
    # The failed attempt already moved the original: its report says so at ERROR.
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    caplog.clear()
    pending = read_incident(data_dir, "notes")
    assert pending is not None
    assert pending["verification"] == "pending"
    assert pending["recovered_at"] is None
    assert (Path(pending["quarantine"]) / "notes.db").read_bytes() == b"damaged"
    # A newer vBot may have added fields meanwhile; they never block completion.
    pending["operator_note"] = {"source": "newer vBot"}
    pending["possible_loss_interval"]["confidence"] = "exact"
    evidence.write_text(json.dumps(pending), encoding="utf-8")

    # The installed newest copy is confirmed; an older snapshot is never tried.
    with caplog.at_level(logging.ERROR, logger="vbot.database"):
        assert stored_bodies(notes_spec(data_dir)) == ["one", "two"]
    [completion] = caplog.records
    assert pending["restored_snapshot_id"] in completion.getMessage()
    assert len(_quarantine_batches(data_dir)) == 1
    completed = read_incident(data_dir, "notes")
    assert completed is not None
    assert completed["verification"] == "ok"
    for kept in ("incident_id", "quarantine", "possible_loss_interval", "operator_note"):
        assert completed[kept] == pending[kept], kept
    assert acknowledge_incident(data_dir, completed["incident_id"]) is True
    assert read_incident(data_dir, "notes") == {**completed, "acknowledged": True}


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
def test_a_rejected_restore_preserves_the_existing_incident(data_dir: Path, rejection: str) -> None:
    incident = _recovered(data_dir)
    snapshot = snapshot_root(data_dir) / incident["restored_snapshot_id"]
    evidence = incident_path(data_dir, "notes").read_bytes()
    path = notes_spec(data_dir).path
    expected: type[Exception]
    if rejection == "hash":

        def tampered(payload: dict[str, Any]) -> None:
            payload["members"]["notes"]["sha256"] = "0" * 64

        rewrite_manifest(snapshot, tampered)
        expected = DatabaseCorruptError
    else:
        expected = DatabaseUnavailableError
    database = open_database(notes_spec(data_dir)) if rejection == "live" else None
    try:
        original = path.read_bytes()
        with pytest.raises(expected):
            restore_data_snapshot(data_dir, snapshot)
        assert path.read_bytes() == original
    finally:
        if database is not None:
            database.close()
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

    monkeypatch.setattr(shutil, "copy2", fail_first_copy)

    with pytest.raises(DatabaseCorruptError):
        open_database(notes_spec(data_dir))
    assert attempted == [newest / "notes.db"]
    assert path.read_bytes() == b"damaged"
    assert not quarantine_root(data_dir).exists()
    assert read_incident(data_dir, "notes") is None


def test_invalid_incident_evidence_is_reported_and_never_rewritten(data_dir: Path) -> None:
    open_database(notes_spec(data_dir)).close()
    evidence = incident_path(data_dir, "notes")
    evidence.parent.mkdir(parents=True, exist_ok=True)

    for serialized in (b"{", b"\xff", b"[]"):
        evidence.write_bytes(serialized)
        with pytest.raises(DatabaseCorruptError):
            open_database(notes_spec(data_dir))
        with pytest.raises(DatabaseCorruptError):
            acknowledge_incident(data_dir, "a" * 32)
        assert evidence.read_bytes() == serialized


def test_a_non_canonical_incident_timestamp_is_invalid_evidence(data_dir: Path) -> None:
    incident = _recovered(data_dir)
    evidence = incident_path(data_dir, "notes")
    # An incident is written with canonical timestamps only; another form is
    # damage, reported and preserved like any unreadable incident.
    for field in ("restored_snapshot_time", "recovered_at", "interval.start", "interval.end"):
        for value in ("2026-09-01T10:00:00Z", "2026-09-01T12:00:00.000000+02:00"):
            changed = json.loads(json.dumps(incident))
            if field.startswith("interval."):
                changed["possible_loss_interval"][field.removeprefix("interval.")] = value
            else:
                changed[field] = value
            serialized = json.dumps(changed).encode("utf-8")
            evidence.write_bytes(serialized)

            with pytest.raises(DatabaseCorruptError, match="invalid timestamp"):
                read_incident(data_dir, "notes")
            with pytest.raises(DatabaseCorruptError):
                acknowledge_incident(data_dir, incident["incident_id"])
            assert evidence.read_bytes() == serialized


@pytest.mark.parametrize("partial_install", [False, True])
def test_a_restore_retry_keeps_the_original_quarantine_and_failure_time(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, partial_install: bool
) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged original")

    with monkeypatch.context() as patched:
        patched.setattr(
            os,
            "replace",
            _failing_replace(lambda _source, target: target == path, PermissionError),
        )
        with pytest.raises(DatabaseCorruptError):
            open_database(notes_spec(data_dir))
    pending = read_incident(data_dir, "notes")
    assert pending is not None
    assert pending["verification"] == "pending"
    assert not path.exists()
    if partial_install:
        path.write_bytes(b"damaged retry output")

    assert stored_bodies(notes_spec(data_dir)) == ["saved"]
    completed = read_incident(data_dir, "notes")
    assert completed is not None
    assert completed["verification"] == "ok"
    for kept in ("incident_id", "cause", "possible_loss_interval", "quarantine"):
        assert completed[kept] == pending[kept], kept
    assert (Path(completed["quarantine"]) / "notes.db").read_bytes() == b"damaged original"


def test_a_retry_after_quarantine_rollback_records_the_actual_evidence(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_with_notes(data_dir, "saved")
    path = notes_spec(data_dir).path
    path.write_bytes(b"damaged original")

    with monkeypatch.context() as patched:
        patched.setattr(
            os,
            "replace",
            _failing_replace(lambda _source, target: _into_quarantine(target), PermissionError),
        )
        with pytest.raises(DatabaseCorruptError):
            open_database(notes_spec(data_dir))
    pending = read_incident(data_dir, "notes")
    assert pending is not None
    # Only reserved: the rolled-back quarantine holds no evidence.
    assert not Path(pending["quarantine"]).exists()

    assert stored_bodies(notes_spec(data_dir)) == ["saved"]
    completed = read_incident(data_dir, "notes")
    assert completed is not None
    assert (Path(completed["quarantine"]) / "notes.db").read_bytes() == b"damaged original"


def test_operator_restore_replaces_only_the_selected_members(data_dir: Path) -> None:
    notes = open_database(notes_spec(data_dir))
    tasks = open_database(notes_spec(data_dir, name="tasks"))
    try:
        add_note(notes, "saved note")
        add_note(tasks, "saved task")
        snapshot = create_data_snapshot(data_dir, reason="test", databases=(notes, tasks))
        assert snapshot is not None
        add_note(notes, "later note")
        add_note(tasks, "later task")
    finally:
        notes.close()
        tasks.close()

    assert restore_data_snapshot(data_dir, snapshot, names=["notes"]).databases == ("notes",)

    assert stored_bodies(notes_spec(data_dir)) == ["saved note"]
    assert stored_bodies(notes_spec(data_dir, name="tasks")) == ["saved task", "later task"]
    assert read_incident(data_dir, "tasks") is None
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    assert incident["cause"] == "manual operator restore"
    assert read_maintenance(data_dir) is None


def test_operator_restore_checks_the_request_and_every_selected_member_first(
    data_dir: Path,
) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")
    database = open_database(notes_spec(data_dir))
    add_note(database, "later")
    database.close()
    path = notes_spec(data_dir).path
    original = path.read_bytes()

    assert restore_data_snapshot(data_dir, snapshot, check_only=True).databases == ("notes",)
    with pytest.raises(DatabaseFormatError, match="has no tasks member"):
        restore_data_snapshot(data_dir, snapshot, names=["notes", "tasks"])
    with pytest.raises(ValueError, match="every member"):
        restore_data_snapshot(data_dir, snapshot, names=["notes"], retire_unlisted=True)

    def replaced(payload: dict[str, Any]) -> None:
        payload["databases"]["notes"]["database_id"] = "f" * 32

    rewrite_marker(data_dir, replaced)
    with pytest.raises(DatabaseFormatError, match="does not belong"):
        restore_data_snapshot(data_dir, snapshot)
    (data_dir / MARKER_FILE_NAME).unlink()
    with pytest.raises(DatabaseFormatError, match="does not authorize"):
        restore_data_snapshot(data_dir, snapshot)

    assert path.read_bytes() == original
    assert read_maintenance(data_dir) is None
    assert read_incident(data_dir, "notes") is None


def test_the_restore_id_changes_with_each_restore_and_survives_reopening(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "saved")

    def restore_id() -> str | None:
        database = open_database(notes_spec(data_dir))
        try:
            return database.restore_id
        finally:
            database.close()

    assert restore_id() is None
    assert restore_id() is None
    restore_data_snapshot(data_dir, snapshot)
    operator = restore_id()
    assert operator is not None
    assert acknowledge_incident(data_dir, operator) is True
    assert restore_id() == operator
    # A snapshot copy carries the database identity but never the restore id.
    notes_spec(data_dir).path.write_bytes(b"damaged")
    automatic = restore_id()
    assert automatic not in (None, operator)
    assert restore_id() == automatic


def _recovered(data_dir: Path) -> dict[str, Any]:
    snapshot_with_notes(data_dir, "saved")
    notes_spec(data_dir).path.write_bytes(b"damaged")
    open_database(notes_spec(data_dir)).close()
    incident = read_incident(data_dir, "notes")
    assert incident is not None
    return incident


def test_acknowledging_acknowledges_exactly_the_observed_incident_durably(
    data_dir: Path,
) -> None:
    assert acknowledge_incident(data_dir, "a" * 32) is False
    incident = _recovered(data_dir)
    evidence = incident_path(data_dir, "notes").read_bytes()

    with pytest.raises(IncidentConflictError):
        acknowledge_incident(data_dir, "0" * 32)
    assert incident_path(data_dir, "notes").read_bytes() == evidence
    assert acknowledge_incident(data_dir, incident["incident_id"]) is True
    assert acknowledge_incident(data_dir, incident["incident_id"]) is True

    assert read_incident(data_dir, "notes") == {**incident, "acknowledged": True}
    assert active_incidents(data_dir) == []
    assert data_store_status(data_dir)["state"] == "healthy"


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
    monkeypatch.setattr("core.database.marker.OPERATION_LOCK_TIMEOUT_SECONDS", 0.05)
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


_EXTENSION = "ext.demo.notes"


def _registered(data_dir: Path) -> set[str]:
    marker = read_marker(data_dir)
    assert marker is not None
    return set(marker.databases)


def test_unregistering_an_extension_database_quarantines_it_and_drops_the_entry(
    data_dir: Path,
) -> None:
    snapshot_with_notes(data_dir, "core")
    snapshot = snapshot_with_notes(data_dir, "extension", name=_EXTENSION)
    path = notes_spec(data_dir, name=_EXTENSION).path
    original = path.read_bytes()
    database_id = _database_id(data_dir, _EXTENSION)

    released = unregister_database(data_dir, _EXTENSION)

    assert released.name == _EXTENSION
    assert released.database_id == database_id
    assert released.quarantine is not None
    assert (released.quarantine / path.name).read_bytes() == original
    assert not path.exists()
    assert _registered(data_dir) == {"notes"}
    # The earlier snapshot keeps its copy; snapshots no longer wait for it.
    assert snapshot.is_dir()
    assert create_data_snapshot(data_dir, reason="test") is not None
    # A database that is no longer registered is never restored automatically.
    assert (
        auto_restore_if_needed(data_dir, notes_spec(data_dir, name=_EXTENSION), database_id)
        is False
    )
    assert not path.exists()


def test_unregistering_releases_a_registration_whose_file_is_gone(data_dir: Path) -> None:
    snapshot_with_notes(data_dir, "extension", name=_EXTENSION)
    notes_spec(data_dir, name=_EXTENSION).path.unlink()

    released = unregister_database(data_dir, _EXTENSION)

    assert released.quarantine is None
    assert _registered(data_dir) == set()


def test_unregistering_is_refused_for_core_unknown_open_or_maintenance(
    data_dir: Path,
) -> None:
    snapshot_with_notes(data_dir, "core")
    snapshot_with_notes(data_dir, "extension", name=_EXTENSION)
    path = notes_spec(data_dir, name=_EXTENSION).path
    original = path.read_bytes()

    with pytest.raises(ValueError, match="notes is a core vBot database"):
        unregister_database(data_dir, "notes")
    with pytest.raises(ValueError, match=f"registered Extension databases: {_EXTENSION}"):
        unregister_database(data_dir, "ext.demo.other")
    with pytest.raises(ValueError, match="invalid database name"):
        unregister_database(data_dir, "../notes")
    database = open_database(notes_spec(data_dir, name=_EXTENSION))
    try:
        with pytest.raises(DatabaseUnavailableError, match="disable its Extension"):
            unregister_database(data_dir, _EXTENSION)
    finally:
        database.close()
    with (
        maintenance(data_dir, "restore"),
        pytest.raises(DatabaseFormatError, match="maintenance is incomplete"),
    ):
        unregister_database(data_dir, _EXTENSION)

    assert path.read_bytes() == original
    assert _registered(data_dir) == {"notes", _EXTENSION}
    assert not quarantine_root(data_dir).exists()


def test_retiring_a_core_database_releases_it_once_and_leaves_open_ones_alone(
    data_dir: Path,
) -> None:
    snapshot_with_notes(data_dir, "core")
    path = notes_spec(data_dir).path
    original = path.read_bytes()
    database_id = _database_id(data_dir, "notes")

    database = open_database(notes_spec(data_dir))
    try:
        with pytest.raises(DatabaseUnavailableError, match="notes database is open"):
            retire_core_databases(data_dir, ("notes",))
    finally:
        database.close()
    with pytest.raises(ValueError, match="use unregister_database"):
        retire_core_databases(data_dir, (_EXTENSION,))
    assert _registered(data_dir) == {"notes"}

    (retired,) = retire_core_databases(data_dir, ("notes", "never_registered"))

    assert (retired.name, retired.database_id) == ("notes", database_id)
    assert retired.quarantine is not None
    assert (retired.quarantine / path.name).read_bytes() == original
    assert not path.exists()
    assert _registered(data_dir) == set()
    assert retire_core_databases(data_dir, ("notes",)) == ()


def _extension_with_sidecar(data_dir: Path) -> tuple[Path, Path, bytes]:
    """A registered Extension database whose bundle also holds a WAL sidecar."""
    open_database(notes_spec(data_dir, name=_EXTENSION)).close()
    path = notes_spec(data_dir, name=_EXTENSION).path
    sidecar = Path(f"{path}-wal")
    sidecar.write_bytes(b"wal evidence")
    return path, sidecar, path.read_bytes()


def test_a_failed_quarantine_rollback_keeps_the_only_copy_in_the_batch(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, sidecar, original = _extension_with_sidecar(data_dir)
    monkeypatch.setattr(
        os,
        "replace",
        # The sidecar cannot move, and moving the database back fails too.
        _failing_replace(lambda source, target: source == sidecar or target == path),
    )

    with pytest.raises(DatabaseUnavailableError, match="rollback failed; retained bundle at"):
        unregister_database(data_dir, _EXTENSION)

    monkeypatch.undo()
    (batch,) = _quarantine_batches(data_dir, _EXTENSION)
    assert (batch / path.name).read_bytes() == original
    assert not path.exists()
    assert sidecar.read_bytes() == b"wal evidence"
    assert _registered(data_dir) == {_EXTENSION}


def test_a_quarantine_durability_failure_moves_the_whole_bundle_back(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, sidecar, original = _extension_with_sidecar(data_dir)
    batches = quarantine_root(data_dir) / _EXTENSION
    real_fsync = recovery_module.fsync_dir

    def fail_quarantine_sync(directory: Path) -> None:
        if directory.parent == batches:
            raise OSError("injected quarantine durability failure")
        real_fsync(directory)

    monkeypatch.setattr(recovery_module, "fsync_dir", fail_quarantine_sync)
    with pytest.raises(DatabaseUnavailableError, match="injected quarantine durability failure"):
        unregister_database(data_dir, _EXTENSION)

    assert path.read_bytes() == original
    assert sidecar.read_bytes() == b"wal evidence"
    assert _quarantine_batches(data_dir, _EXTENSION) == []
    assert _registered(data_dir) == {_EXTENSION}


def test_restoring_an_unregistered_member_registers_it_again(data_dir: Path) -> None:
    snapshot = snapshot_with_notes(data_dir, "extension", name=_EXTENSION)
    database_id = _database_id(data_dir, _EXTENSION)
    unregister_database(data_dir, _EXTENSION)

    planned = restore_data_snapshot(data_dir, snapshot, names=[_EXTENSION], check_only=True)
    assert planned.registered == (_EXTENSION,)
    assert _registered(data_dir) == set()

    restored = restore_data_snapshot(data_dir, snapshot, names=[_EXTENSION])

    assert restored.databases == (_EXTENSION,)
    assert restored.registered == (_EXTENSION,)
    assert _database_id(data_dir, _EXTENSION) == database_id
    assert stored_bodies(notes_spec(data_dir, name=_EXTENSION)) == ["extension"]
    assert read_maintenance(data_dir) is None
