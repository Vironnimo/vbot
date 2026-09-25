"""Data-store RPC catalog and operator-state contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.database import (
    UnregisteredDatabase,
    open_database,
    read_marker,
    unregister_database,
    write_bootstrap_marker,
)
from core.database import snapshots as snapshots_module
from core.database.recovery import write_incident
from core.sessions import ChatSessionManager
from server.events import ServerEventBus
from server.rpc.methods import build_method_handlers, dispatch_rpc
from tests.core.database.database_test_support import notes_spec

_EXTENSION = "ext.demo.notes"


def _state(data_dir: Path, sessions: ChatSessionManager) -> SimpleNamespace:
    async def unregister_extension_database(name: str) -> UnregisteredDatabase:
        return unregister_database(data_dir, name)

    return SimpleNamespace(
        runtime=SimpleNamespace(
            canonical_databases=lambda: (sessions.database,),
            storage=SimpleNamespace(data_dir=data_dir),
            unregister_extension_database=unregister_extension_database,
        ),
        event_bus=ServerEventBus(),
    )


def test_data_store_status_and_incident_methods_are_publicly_catalogued() -> None:
    handlers = build_method_handlers()

    assert {
        "data_store.status",
        "data_store.snapshot_create",
        "data_store.incident_acknowledge",
        "data_store.unregister",
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
            restored_snapshot_time="2026-08-31T10:00:00.000000Z",
            failure_detected_at="2026-08-31T10:05:00.000000Z",
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


@pytest.mark.asyncio
async def test_snapshot_create_names_why_no_snapshot_was_taken(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    open_database(notes_spec(tmp_path, name=_EXTENSION)).close()
    notes_spec(tmp_path, name=_EXTENSION).path.unlink()
    try:
        response = await dispatch_rpc(
            _state(tmp_path, sessions),
            {"method": "data_store.snapshot_create", "params": {"reason": "manual"}},
        )
    finally:
        sessions.close()

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"
    assert response["error"]["message"].startswith(
        "the data snapshot was not created: data snapshots need every registered database: "
        f"the registered Extension database {_EXTENSION} has no file"
    )
    assert f"vbot data-store unregister {_EXTENSION} --yes" in response["error"]["message"]


@pytest.mark.asyncio
async def test_unregister_releases_an_extension_database(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    open_database(notes_spec(tmp_path, name=_EXTENSION)).close()
    path = notes_spec(tmp_path, name=_EXTENSION).path
    state = _state(tmp_path, sessions)
    try:
        response = await dispatch_rpc(
            state, {"method": "data_store.unregister", "params": {"name": _EXTENSION}}
        )
    finally:
        sessions.close()

    assert response["ok"] is True
    released = response["result"]["unregistered"]
    assert released["name"] == _EXTENSION
    assert released["database_id"]
    assert (Path(released["quarantine"]) / path.name).is_file()
    assert not path.exists()
    marker = read_marker(tmp_path)
    assert marker is not None
    assert set(marker.databases) == {"sessions"}
    assert state.event_bus.events[-1]["payload"] == {"kind": "data_store"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"name": "sessions"}, "sessions is a core vBot database and cannot be unregistered"),
        ({"name": "ext.demo.other"}, "ext.demo.other is not a registered database"),
        ({}, "params.name must be a non-empty string"),
        ({"name": _EXTENSION, "force": True}, "force"),
    ],
)
async def test_unregister_refuses_core_unknown_and_malformed_requests(
    tmp_path: Path, params: dict[str, Any], message: str
) -> None:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    open_database(notes_spec(tmp_path, name=_EXTENSION)).close()
    state = _state(tmp_path, sessions)
    try:
        response = await dispatch_rpc(state, {"method": "data_store.unregister", "params": params})
    finally:
        sessions.close()

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert message in response["error"]["message"]
    marker = read_marker(tmp_path)
    assert marker is not None
    assert set(marker.databases) == {"sessions", _EXTENSION}
    assert notes_spec(tmp_path, name=_EXTENSION).path.is_file()
    assert state.event_bus.events == []
