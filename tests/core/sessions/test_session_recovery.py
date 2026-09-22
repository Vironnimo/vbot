"""Regression contracts for Session recovery safety."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions import recovery as recovery_module
from core.sessions import snapshots as snapshots_module
from core.sessions.errors import SessionStoreCorruptError
from core.sessions.format import read_session_store_marker
from core.sessions.sqlite_runtime import has_live_connection


def _create_verified_snapshot(tmp_path: Path) -> tuple[Path, SessionAddress, ChatMessage]:
    address = SessionAddress(project_id=None, agent_id="agent", session_id="recover")
    message = ChatMessage.user("recoverable history")
    sessions = ChatSessionManager(tmp_path)
    sessions.create(address.agent_id, session_id=address.session_id).append(message)
    marker = read_session_store_marker(tmp_path)
    assert marker is not None
    snapshot = snapshots_module.create_snapshot(
        tmp_path,
        tmp_path / "sessions.db",
        sessions.backup_snapshot,
        database_id=str(marker["database_id"]),
    )
    sessions.close()
    assert snapshot is not None
    return snapshot, address, message


@pytest.mark.parametrize("mode", ["manual", "incident", "automatic"])
def test_restore_rejects_incompatible_shape_before_touching_canonical_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from core.sessions import schema

    snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    # The application changes its table contract while retaining schema version 1.
    monkeypatch.setattr(
        schema,
        "SCHEMA_SQL",
        schema.SCHEMA_SQL.replace(
            "status IN ('live', 'archived')", "status IN ('live', 'archived', 'test')"
        ),
    )
    if mode == "automatic":
        database.write_bytes(b"damaged canonical database")
    original = database.read_bytes()
    if mode == "manual":
        result = recovery_module.restore_snapshot(tmp_path, database, snapshot)
    elif mode == "incident":
        result = recovery_module.restore_snapshot_with_incident(
            tmp_path, database, snapshot, cause="test"
        )
    else:
        result = recovery_module.auto_restore_if_needed(tmp_path, database)
    assert result is False
    assert database.read_bytes() == original
    assert not (tmp_path / "session-quarantine").exists()
    assert recovery_module.read_recovery_incident(tmp_path) is None


def test_identity_mismatch_recovers_from_a_compatible_snapshot_and_closes_connections(
    tmp_path: Path,
) -> None:
    snapshot, address, message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE store_meta SET value = ? WHERE key = 'database_id'", ("b" * 32,))
        connection.commit()
    finally:
        connection.close()

    recovered = ChatSessionManager(tmp_path)
    try:
        assert [item.content for item in recovered.get(address).load()] == [message.content]
    finally:
        recovered.close()
    assert not has_live_connection(database)


def test_failed_quarantine_prevents_snapshot_replacement_and_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    original = database.read_bytes()
    real_replace = snapshots_module.os.replace

    def fail_quarantine(source: str | Path, destination: str | Path) -> None:
        if "session-quarantine" in str(destination):
            raise OSError("injected quarantine failure")
        real_replace(source, destination)

    monkeypatch.setattr(snapshots_module.os, "replace", fail_quarantine)

    with pytest.raises(recovery_module.SessionStoreUnavailableError):
        recovery_module.restore_snapshot(tmp_path, database, snapshot)
    assert database.read_bytes() == original


def test_incident_publication_failure_does_not_report_recovery_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged")
    real_replace = snapshots_module.os.replace

    def fail_incident(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "session-recovery.json":
            raise OSError("injected incident publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(snapshots_module.os, "replace", fail_incident)

    with pytest.raises(SessionStoreCorruptError):
        ChatSessionManager(tmp_path)


def test_final_incident_failure_leaves_pending_evidence_and_retries_on_next_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _snapshot, address, message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged")
    real_replace = snapshots_module.os.replace
    incident_replaces = 0

    def fail_final_incident(source: str | Path, destination: str | Path) -> None:
        nonlocal incident_replaces
        if Path(destination).name == "session-recovery.json":
            incident_replaces += 1
            if incident_replaces == 2:
                raise OSError("injected final incident publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(snapshots_module.os, "replace", fail_final_incident)
    with pytest.raises(SessionStoreCorruptError):
        ChatSessionManager(tmp_path)
    pending = recovery_module.read_recovery_incident(tmp_path)
    assert pending is not None
    assert pending["verification"] == "pending"
    assert Path(pending["quarantine"]).is_dir()
    assert (Path(pending["quarantine"]) / "sessions.db").read_bytes() == b"damaged"

    monkeypatch.setattr(snapshots_module.os, "replace", real_replace)
    reopened = ChatSessionManager(tmp_path)
    try:
        assert [item.content for item in reopened.get(address).load()] == [message.content]
    finally:
        reopened.close()
    completed = recovery_module.read_recovery_incident(tmp_path)
    assert completed is not None
    assert completed["incident_id"] == pending["incident_id"]
    assert completed["verification"] == "ok"
    assert completed["quarantine"] == pending["quarantine"]


def test_recovery_lock_is_not_broken_by_wall_clock_age(tmp_path: Path) -> None:
    lock = snapshots_module.snapshot_root(tmp_path) / snapshots_module.SNAPSHOT_LOCK_NAME
    lock.parent.mkdir(parents=True)
    owner = snapshots_module._acquire_lock(lock)
    assert owner is not None
    old = lock.stat().st_mtime - 120
    import os

    os.utime(lock, (old, old))
    try:
        assert snapshots_module._acquire_lock(lock, timeout=0.05) is None
    finally:
        snapshots_module._release_lock(owner)


def test_failed_quarantine_rollback_keeps_the_only_database_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "sessions.db"
    sidecar = tmp_path / "sessions.db-wal"
    database.write_bytes(b"original database")
    sidecar.write_bytes(b"original wal")
    real_replace = recovery_module.os.replace

    def fail_sidecar_and_rollback(source: str | Path, destination: str | Path) -> None:
        if Path(source) == sidecar or Path(destination) == database:
            raise OSError("injected move failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_sidecar_and_rollback)
    result = recovery_module.quarantine_database(database)
    assert result.status == "failed"
    assert result.path is not None
    assert (result.path / database.name).read_bytes() == b"original database"
    assert sidecar.read_bytes() == b"original wal"


def test_final_incident_failure_never_restores_an_older_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older, address, first_message = _create_verified_snapshot(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    newer_message = ChatMessage.user("newer recoverable history")
    sessions.get(address).append(newer_message)
    marker = read_session_store_marker(tmp_path)
    assert marker is not None
    newest = snapshots_module.create_snapshot(
        tmp_path,
        tmp_path / "sessions.db",
        sessions.backup_snapshot,
        database_id=str(marker["database_id"]),
    )
    sessions.close()
    assert newest is not None
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged")
    monkeypatch.setattr(snapshots_module, "list_snapshots", lambda *a, **kw: [newest, older])
    real_publish = recovery_module.write_recovery_incident

    def fail_final_incident(*args, **kwargs):
        if kwargs.get("verification") == "ok":
            raise recovery_module.SessionStoreUnavailableError("injected finalization failure")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(recovery_module, "write_recovery_incident", fail_final_incident)
    assert recovery_module.auto_restore_if_needed(tmp_path, database) is False
    monkeypatch.setattr(recovery_module, "write_recovery_incident", real_publish)
    reopened = ChatSessionManager(tmp_path)
    try:
        assert [message.content for message in reopened.get(address).load()] == [
            first_message.content,
            newer_message.content,
        ]
    finally:
        reopened.close()
    assert len(list((tmp_path / "session-quarantine").iterdir())) == 1


def test_concurrent_recovery_reprobes_after_the_first_owner_finishes(tmp_path: Path) -> None:
    _snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def recover() -> None:
        barrier.wait()
        results.append(recovery_module.auto_restore_if_needed(tmp_path, database))

    workers = [threading.Thread(target=recover) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    quarantine_root = tmp_path / "session-quarantine"
    assert sorted(results) == [False, True]
    assert len(list(quarantine_root.iterdir())) == 1


@pytest.mark.parametrize("rejection", ["hash", "live"])
def test_rejected_restore_preserves_existing_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rejection: str
) -> None:
    snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    recovery_module.write_recovery_incident(
        tmp_path,
        cause="earlier recovery",
        quarantine_path=None,
        restored_snapshot_id="earlier",
        restored_snapshot_time="2026-08-31T10:00:00Z",
    )
    incident_path = tmp_path / "session-recovery.json"
    evidence = incident_path.read_bytes()
    if rejection == "hash":
        import json

        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        from core.sessions import sqlite_runtime

        monkeypatch.setattr(sqlite_runtime, "has_live_connection", lambda path: True)
    original = database.read_bytes()
    assert (
        recovery_module.restore_snapshot_with_incident(
            tmp_path, database, snapshot, cause="rejected restore"
        )
        is False
    )
    assert database.read_bytes() == original
    assert incident_path.read_bytes() == evidence


def test_copy_failure_stops_candidate_fallback_and_preserves_canonical_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    newest, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged canonical database")
    attempted: list[Path] = []
    real_copy = shutil.copy2

    def fail_first_copy(source, destination, *args, **kwargs):
        attempted.append(Path(source))
        if len(attempted) == 1:
            raise OSError("injected temporary write failure")
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", fail_first_copy)
    monkeypatch.setattr(snapshots_module, "list_snapshots", lambda *a, **kw: [newest, newest])
    recovery_module.auto_restore_if_needed(tmp_path, database)
    assert attempted == [newest / "sessions.db"]
    assert database.read_bytes() == b"damaged canonical database"
    assert not (tmp_path / "session-quarantine").exists()
    assert recovery_module.read_recovery_incident(tmp_path) is None


@pytest.mark.parametrize(
    "serialized", [b"{", b"\xff", b"[]", b'{"incident_id":"a","acknowledged":false}']
)
def test_store_open_preserves_invalid_incident_evidence(tmp_path: Path, serialized: bytes) -> None:
    _snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    incident_path = tmp_path / "session-recovery.json"
    incident_path.write_bytes(serialized)
    with pytest.raises(SessionStoreCorruptError):
        ChatSessionManager(tmp_path)
    assert incident_path.read_bytes() == serialized


def test_schema_shape_corruption_recovers_from_compatible_snapshot(tmp_path: Path) -> None:
    _snapshot, address, message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, ?, ?) WHERE name = 'sessions'",
            ("status IN ('live', 'archived')", "status IN ('live', 'archived', 'invalid')"),
        )
        connection.commit()
    finally:
        connection.close()
    assert recovery_module.auto_restore_if_needed(tmp_path, database) is True
    sessions = ChatSessionManager(tmp_path)
    try:
        assert [item.content for item in sessions.get(address).load()] == [message.content]
    finally:
        sessions.close()


def test_pending_restore_does_not_mistake_untouched_original_for_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, address, _message = _create_verified_snapshot(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.get(address).append(ChatMessage.user("newer original history"))
    sessions.close()
    database = tmp_path / "sessions.db"
    original = database.read_bytes()
    real_replace = recovery_module.os.replace

    def fail_quarantine(source, destination):
        if Path(destination).parent.parent.name == "session-quarantine":
            raise PermissionError("injected quarantine access failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_quarantine)
    with pytest.raises(recovery_module.SessionStoreUnavailableError):
        recovery_module.restore_snapshot_with_incident(tmp_path, database, snapshot, cause="test")
    pending = recovery_module.read_recovery_incident(tmp_path)
    assert pending is not None and pending["verification"] == "pending"
    monkeypatch.setattr(recovery_module.os, "replace", real_replace)
    assert recovery_module.auto_restore_if_needed(tmp_path, database) is False
    assert database.read_bytes() == original
    assert recovery_module.read_recovery_incident(tmp_path) == pending


@pytest.mark.parametrize("partial_install", [False, True])
def test_restore_retry_preserves_original_quarantine_and_failure_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial_install: bool
) -> None:
    snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged original")
    real_replace = recovery_module.os.replace

    def fail_install(source, destination):
        if Path(destination) == database:
            raise PermissionError("injected restore publication failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_install)
    with pytest.raises(recovery_module.SessionStoreUnavailableError):
        recovery_module.restore_snapshot_with_incident(tmp_path, database, snapshot, cause="test")
    pending = recovery_module.read_recovery_incident(tmp_path)
    assert pending is not None
    assert not database.exists()
    if partial_install:
        database.write_bytes(b"damaged retry output")
    monkeypatch.setattr(recovery_module.os, "replace", real_replace)
    assert recovery_module.auto_restore_if_needed(tmp_path, database) is True
    completed = recovery_module.read_recovery_incident(tmp_path)
    assert completed is not None
    assert completed["incident_id"] == pending["incident_id"]
    assert completed["possible_loss_interval"] == pending["possible_loss_interval"]
    assert completed["quarantine"] == pending["quarantine"]
    assert (Path(completed["quarantine"]) / "sessions.db").read_bytes() == b"damaged original"


def test_quarantine_durability_failure_rolls_back_the_complete_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "sessions.db"
    sidecar = tmp_path / "sessions.db-wal"
    database.write_bytes(b"database evidence")
    sidecar.write_bytes(b"wal evidence")
    real_fsync = snapshots_module._fsync_dir

    def fail_quarantine_sync(path):
        if path.parent.name == "session-quarantine":
            raise OSError("injected quarantine durability failure")
        real_fsync(path)

    monkeypatch.setattr(snapshots_module, "_fsync_dir", fail_quarantine_sync)
    result = recovery_module.quarantine_database(database)
    assert result.status == "failed"
    assert database.read_bytes() == b"database evidence"
    assert sidecar.read_bytes() == b"wal evidence"


def test_retry_after_quarantine_rollback_records_the_actual_evidence_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged original")
    real_replace = recovery_module.os.replace

    def fail_quarantine(source, destination):
        if Path(destination).parent.parent.name == "session-quarantine":
            raise PermissionError("injected quarantine failure")
        real_replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_quarantine)
    with pytest.raises(recovery_module.SessionStoreUnavailableError):
        recovery_module.restore_snapshot_with_incident(tmp_path, database, snapshot, cause="test")
    pending = recovery_module.read_recovery_incident(tmp_path)
    assert pending is not None
    assert not Path(pending["quarantine"]).exists()
    monkeypatch.setattr(recovery_module.os, "replace", real_replace)
    assert recovery_module.auto_restore_if_needed(tmp_path, database) is True
    completed = recovery_module.read_recovery_incident(tmp_path)
    assert completed is not None
    assert (Path(completed["quarantine"]) / "sessions.db").read_bytes() == b"damaged original"
