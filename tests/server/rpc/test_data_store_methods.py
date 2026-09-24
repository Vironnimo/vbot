"""Data-store RPC catalog and operator-state contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.database import snapshots as snapshots_module
from core.database import write_bootstrap_marker
from core.database.recovery import write_incident
from core.sessions import ChatSessionManager
from server.events import ServerEventBus
from server.rpc.methods import build_method_handlers, dispatch_rpc


def _state(data_dir: Path, sessions: ChatSessionManager) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            canonical_databases=lambda: (sessions.database,),
            storage=SimpleNamespace(data_dir=data_dir),
        ),
        event_bus=ServerEventBus(),
    )


def test_data_store_status_and_incident_methods_are_publicly_catalogued() -> None:
    handlers = build_method_handlers()

    assert {
        "data_store.status",
        "data_store.snapshot_create",
        "data_store.incident_acknowledge",
    } <= handlers.keys()


@pytest.mark.asyncio
async def test_status_and_incident_acknowledgement_are_operator_safe(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    state = _state(tmp_path, sessions)
    try:
        status = await dispatch_rpc(state, {"method": "data_store.status", "params": {}})
        assert status["ok"] is True
        assert status["result"]["state"] == "snapshot_degraded"
        assert set(status["result"]["databases"]) == {"sessions"}
        assert status["result"]["databases"]["sessions"]["state"] == "healthy"

        created = await dispatch_rpc(
            state,
            {"method": "data_store.snapshot_create", "params": {"reason": "manual"}},
        )
        assert created["ok"] is True
        assert created["result"]["snapshot"]["reason"] == "manual"
        assert set(created["result"]["snapshot"]["members"]) == {"sessions"}
        assert state.event_bus.events[-1]["payload"] == {"kind": "data_store"}

        write_incident(
            tmp_path,
            "sessions",
            cause="test-corruption",
            quarantine_path=tmp_path / "quarantine" / "sessions" / "bundle",
            restored_snapshot_id="snapshot-1",
            restored_snapshot_time="2026-08-31T10:00:00Z",
            failure_detected_at="2026-08-31T10:05:00Z",
        )
        incident_status = await dispatch_rpc(state, {"method": "data_store.status", "params": {}})
        incident = incident_status["result"]["incidents"][0]
        assert incident["database"] == "sessions"
        assert incident_status["result"]["state"] == "recovered_with_incident"

        stale = await dispatch_rpc(
            state,
            {"method": "data_store.incident_acknowledge", "params": {"incident_id": "stale"}},
        )
        assert stale["ok"] is False

        acknowledged = await dispatch_rpc(
            state,
            {
                "method": "data_store.incident_acknowledge",
                "params": {"incident_id": incident["incident_id"]},
            },
        )
        assert acknowledged["ok"] is True
        assert acknowledged["result"]["state"] == "healthy"
        assert acknowledged["result"]["incidents"] == []
        assert state.event_bus.events[-1]["payload"] == {"kind": "data_store"}
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_snapshot_create_rejects_unknown_reasons(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    try:
        response = await dispatch_rpc(
            _state(tmp_path, sessions),
            {"method": "data_store.snapshot_create", "params": {"reason": "nightly"}},
        )
    finally:
        sessions.close()

    assert response["ok"] is False


@pytest.mark.asyncio
async def test_snapshot_create_returns_without_reverifying_the_snapshot_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    state = _state(tmp_path, sessions)
    sha256_calls: list[Path] = []
    verification_calls: list[Path] = []
    real_sha256 = snapshots_module._sha256
    real_verify = snapshots_module.verify_database_file

    def counted_sha256(path: Path, **kwargs: Any) -> str:
        sha256_calls.append(path)
        return real_sha256(path, **kwargs)

    def counted_verify(path: Path, *args: Any, **kwargs: Any) -> Any:
        verification_calls.append(path)
        return real_verify(path, *args, **kwargs)

    monkeypatch.setattr(snapshots_module, "_sha256", counted_sha256)
    monkeypatch.setattr(snapshots_module, "verify_database_file", counted_verify)
    try:
        created = await dispatch_rpc(
            state,
            {"method": "data_store.snapshot_create", "params": {"reason": "manual"}},
        )

        assert created["ok"] is True
        assert created["result"]["snapshot"]["reason"] == "manual"
        assert len(sha256_calls) == 1
        assert len(verification_calls) == 1
    finally:
        sessions.close()
