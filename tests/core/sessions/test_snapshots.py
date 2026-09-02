"""Strict snapshot manifest and recovery-state contracts."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.sessions import ChatSessionManager
from core.sessions import snapshots as snapshots_module
from core.sessions.errors import SessionStoreCorruptError, SessionStoreUnavailableError
from core.sessions.format import read_session_store_marker
from core.sessions.snapshots import (
    SNAPSHOT_MANIFEST_NAME,
    acknowledge_recovery_incident,
    create_snapshot,
    list_snapshots,
    read_recovery_incident,
    snapshot_summaries,
    write_recovery_incident,
)


def _snapshot(tmp_path: Path) -> tuple[ChatSessionManager, Path]:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("agent", session_id="snapshot").append(ChatMessage.user("retained"))
    marker = read_session_store_marker(tmp_path)
    assert marker is not None
    snapshot = create_snapshot(
        tmp_path,
        tmp_path / "sessions.db",
        sessions.backup_snapshot,
        database_id=str(marker["database_id"]),
        reason="test",
    )
    assert snapshot is not None
    return sessions, snapshot


def test_snapshot_manifest_is_strict_and_operator_safe(tmp_path: Path) -> None:
    sessions, snapshot = _snapshot(tmp_path)
    try:
        payload = json.loads((snapshot / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        assert set(payload) == {
            "manifest_version",
            "snapshot_id",
            "reason",
            "created_at",
            "database_id",
            "schema_version",
            "application_id",
            "sqlite_version",
            "sqlite_source_id",
            "file_size",
            "sha256",
            "session_count",
            "message_count",
            "latest_history_revision",
            "latest_state_revision",
            "integrity",
            "foreign_key_check",
            "database_file",
            "complete",
        }
        assert list_snapshots(tmp_path) == [snapshot]
        summary = snapshot_summaries(tmp_path)[0]
        assert summary["snapshot_id"] == snapshot.name
        assert "retained" not in json.dumps(summary)
    finally:
        sessions.close()


def test_unverified_manifest_is_not_a_restore_candidate(tmp_path: Path) -> None:
    sessions, snapshot = _snapshot(tmp_path)
    try:
        manifest_path = snapshot / SNAPSHOT_MANIFEST_NAME
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["unexpected"] = True
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        assert list_snapshots(tmp_path) == []
    finally:
        sessions.close()


def test_acknowledgement_is_durable_and_preserves_evidence(tmp_path: Path) -> None:
    write_recovery_incident(
        tmp_path,
        cause="test-corruption",
        quarantine_path=tmp_path / "session-quarantine" / "bundle",
        restored_snapshot_id="snapshot-1",
        restored_snapshot_time="2026-08-31T10:00:00Z",
        failure_detected_at="2026-08-31T10:05:00Z",
    )
    incident = read_recovery_incident(tmp_path)
    assert incident is not None
    incident_id = str(incident["incident_id"])

    assert acknowledge_recovery_incident(tmp_path, incident_id) is True
    acknowledged = read_recovery_incident(tmp_path)
    assert acknowledged is not None
    assert acknowledged["incident_id"] == incident_id
    assert acknowledged["acknowledged"] is True
    assert (tmp_path / "session-recovery.json").is_file()


@pytest.mark.parametrize("serialized", [b"{", b"\xff"])
def test_acknowledgement_rejects_corrupt_incident_without_changing_evidence(
    tmp_path: Path,
    serialized: bytes,
) -> None:
    incident_path = tmp_path / "session-recovery.json"
    incident_path.write_bytes(serialized)

    with pytest.raises(SessionStoreCorruptError):
        acknowledge_recovery_incident(tmp_path, "observed")

    assert incident_path.read_bytes() == serialized


def test_acknowledgement_reports_unreadable_incident_without_changing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_recovery_incident(
        tmp_path,
        cause="test-corruption",
        quarantine_path=None,
        restored_snapshot_id="snapshot-1",
        restored_snapshot_time="2026-08-31T10:00:00Z",
    )
    incident_path = tmp_path / "session-recovery.json"
    evidence = incident_path.read_bytes()
    real_read_text = Path.read_text

    def deny_incident_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == incident_path:
            raise PermissionError("injected unreadable incident")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_incident_read)

    with pytest.raises(SessionStoreUnavailableError):
        acknowledge_recovery_incident(tmp_path, "observed")

    assert incident_path.read_bytes() == evidence


def test_acknowledgement_reports_operation_lock_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_recovery_incident(
        tmp_path,
        cause="test-corruption",
        quarantine_path=None,
        restored_snapshot_id="snapshot-1",
        restored_snapshot_time="2026-08-31T10:00:00Z",
    )
    incident_path = tmp_path / "session-recovery.json"
    evidence = incident_path.read_bytes()
    lock_path = snapshots_module.snapshot_root(tmp_path) / snapshots_module.SNAPSHOT_LOCK_NAME
    owner = snapshots_module._acquire_lock(lock_path)
    assert owner is not None
    monkeypatch.setattr(snapshots_module, "SNAPSHOT_LOCK_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(SessionStoreUnavailableError):
            acknowledge_recovery_incident(tmp_path, "observed")
    finally:
        snapshots_module._release_lock(owner)

    assert incident_path.read_bytes() == evidence


def test_acknowledgement_serializes_with_a_recovery_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_recovery_incident(
        tmp_path,
        cause="first-corruption",
        quarantine_path=None,
        restored_snapshot_id="snapshot-1",
        restored_snapshot_time="2026-08-31T10:00:00Z",
        incident_id="first-incident",
    )
    acknowledgement_staged = threading.Event()
    allow_acknowledgement = threading.Event()
    writer_attempting = threading.Event()
    writer_acquired = threading.Event()
    acknowledgement_results: list[bool] = []
    thread_errors: list[BaseException] = []
    real_fsync_file: Callable[[Path], None] = snapshots_module._fsync_file

    def pause_staged_acknowledgement(path: Path) -> None:
        real_fsync_file(path)
        if threading.current_thread().name == "incident-acknowledger" and path.name.startswith(
            ".session-recovery.json."
        ):
            acknowledgement_staged.set()
            if not allow_acknowledgement.wait(timeout=2.0):
                raise TimeoutError("acknowledgement test release timed out")

    def acknowledge() -> None:
        try:
            acknowledgement_results.append(
                acknowledge_recovery_incident(tmp_path, "first-incident")
            )
        except BaseException as exc:
            thread_errors.append(exc)

    def publish_replacement() -> None:
        lock_path = snapshots_module.snapshot_root(tmp_path) / snapshots_module.SNAPSHOT_LOCK_NAME
        writer_attempting.set()
        owner = snapshots_module._acquire_lock(lock_path, timeout=2.0)
        if owner is None:
            thread_errors.append(TimeoutError("recovery writer did not acquire operation lock"))
            return
        writer_acquired.set()
        try:
            write_recovery_incident(
                tmp_path,
                cause="replacement-corruption",
                quarantine_path=None,
                restored_snapshot_id="snapshot-2",
                restored_snapshot_time="2026-08-31T11:00:00Z",
                incident_id="replacement-incident",
            )
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            snapshots_module._release_lock(owner)

    monkeypatch.setattr(snapshots_module, "_fsync_file", pause_staged_acknowledgement)
    acknowledger = threading.Thread(target=acknowledge, name="incident-acknowledger")
    writer = threading.Thread(target=publish_replacement, name="recovery-writer")
    acknowledger.start()
    assert acknowledgement_staged.wait(timeout=2.0)
    writer.start()
    assert writer_attempting.wait(timeout=2.0)
    try:
        assert not writer_acquired.wait(timeout=0.1)
    finally:
        allow_acknowledgement.set()
        acknowledger.join(timeout=2.0)
        writer.join(timeout=2.0)

    assert not acknowledger.is_alive()
    assert not writer.is_alive()
    assert thread_errors == []
    assert acknowledgement_results == [True]
    replacement = read_recovery_incident(tmp_path)
    assert replacement is not None
    assert replacement["incident_id"] == "replacement-incident"
    assert replacement["acknowledged"] is False
