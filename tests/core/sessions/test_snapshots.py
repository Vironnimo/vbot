"""Strict snapshot manifest and recovery-state contracts."""

from __future__ import annotations

import json
from pathlib import Path

from core.chat import ChatMessage
from core.sessions import ChatSessionManager
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
