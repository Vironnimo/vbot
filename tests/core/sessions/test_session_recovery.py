"""Regression contracts for Session recovery safety."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
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

    assert snapshots_module.restore_snapshot(tmp_path, database, snapshot) is False
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
    pending = snapshots_module.read_recovery_incident(tmp_path)
    assert pending is not None
    assert pending["verification"] == "pending"

    monkeypatch.setattr(snapshots_module.os, "replace", real_replace)
    reopened = ChatSessionManager(tmp_path)
    try:
        assert [item.content for item in reopened.get(address).load()] == [message.content]
    finally:
        reopened.close()
    completed = snapshots_module.read_recovery_incident(tmp_path)
    assert completed is not None
    assert completed["incident_id"] == pending["incident_id"]
    assert completed["verification"] == "ok"


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


def test_concurrent_recovery_reprobes_after_the_first_owner_finishes(tmp_path: Path) -> None:
    _snapshot, _address, _message = _create_verified_snapshot(tmp_path)
    database = tmp_path / "sessions.db"
    database.write_bytes(b"damaged")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def recover() -> None:
        barrier.wait()
        results.append(snapshots_module.auto_restore_if_needed(tmp_path, database))

    workers = [threading.Thread(target=recover) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    quarantine_root = tmp_path / "session-quarantine"
    assert sorted(results) == [False, True]
    assert len(list(quarantine_root.iterdir())) == 1
