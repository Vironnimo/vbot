"""Session-store RPC catalog and operator-state contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.sessions import ChatSessionManager
from core.sessions.format import write_bootstrap_marker
from core.sessions.snapshots import write_recovery_incident
from server.events import ServerEventBus
from server.rpc.methods import build_method_handlers, dispatch_rpc


def test_session_store_status_and_incident_methods_are_publicly_catalogued() -> None:
    handlers = build_method_handlers()

    assert {
        "session_store.status",
        "session_store.snapshot_create",
        "session_store.incident_acknowledge",
    } <= handlers.keys()


@pytest.mark.asyncio
async def test_status_and_incident_acknowledgement_are_operator_safe(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    state = SimpleNamespace(
        runtime=SimpleNamespace(
            chat_sessions=sessions,
            storage=SimpleNamespace(data_dir=tmp_path),
        ),
        event_bus=ServerEventBus(),
    )
    try:
        status = await dispatch_rpc(
            state,
            {"method": "session_store.status", "params": {}},
        )
        assert status["ok"] is True
        assert status["result"]["state"] == "ready"
        assert "sessions" not in status["result"]

        write_recovery_incident(
            tmp_path,
            cause="test-corruption",
            quarantine_path=tmp_path / "session-quarantine" / "bundle",
            restored_snapshot_id="snapshot-1",
            restored_snapshot_time="2026-08-31T10:00:00Z",
            failure_detected_at="2026-08-31T10:05:00Z",
        )
        incident_status = await dispatch_rpc(
            state,
            {"method": "session_store.status", "params": {}},
        )
        incident_id = incident_status["result"]["incident"]["incident_id"]
        assert incident_status["result"]["state"] == "recovered_with_incident"

        acknowledged = await dispatch_rpc(
            state,
            {
                "method": "session_store.incident_acknowledge",
                "params": {"incident_id": incident_id},
            },
        )
        assert acknowledged["ok"] is True
        assert acknowledged["result"]["state"] == "ready"
        assert acknowledged["result"]["incident"] is None
        assert state.event_bus.events[-1]["payload"] == {"kind": "session_store"}
    finally:
        sessions.close()
