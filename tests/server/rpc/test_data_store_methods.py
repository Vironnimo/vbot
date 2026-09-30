"""Data-store RPCs: health status, snapshots, incidents and Extension database release."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.database import (
    SnapshotBarrier,
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
from tests.core.database.database_test_support import notes_spec
from tests.server.rpc_test_support import resource_changes, rpc_error, rpc_result

_EXTENSION = "ext.demo.notes"
_DATA_STORE_CHANGED = {"kind": "data_store"}


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[ChatSessionManager]:
    write_bootstrap_marker(tmp_path)
    manager = ChatSessionManager(tmp_path)
    try:
        yield manager
    finally:
        manager.close()


def _state(data_dir: Path, sessions: ChatSessionManager) -> SimpleNamespace:
    async def unregister_extension_database(name: str) -> UnregisteredDatabase:
        return unregister_database(data_dir, name)

    return SimpleNamespace(
        runtime=SimpleNamespace(
            canonical_databases=lambda: (sessions.database,),
            snapshot_barrier=SnapshotBarrier(),
            storage=SimpleNamespace(data_dir=data_dir),
            unregister_extension_database=unregister_extension_database,
        ),
        event_bus=ServerEventBus(),
    )


def _register_extension_database(data_dir: Path) -> Path:
    spec = notes_spec(data_dir, name=_EXTENSION)
    open_database(spec).close()
    return Path(spec.path)


@pytest.mark.asyncio
async def test_status_snapshot_and_incident_acknowledgement_are_operator_safe(
    tmp_path: Path, sessions: ChatSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, sessions)
    hashed: list[Path] = []
    verified: list[Path] = []
    real_sha256 = snapshots_module._sha256
    real_verify = snapshots_module.verify_database_file

    def counted_sha256(path: Path, **kwargs: Any) -> str:
        hashed.append(path)
        return real_sha256(path, **kwargs)

    def counted_verify(path: Path, *args: Any, **kwargs: Any) -> Any:
        verified.append(path)
        return real_verify(path, *args, **kwargs)

    monkeypatch.setattr(snapshots_module, "_sha256", counted_sha256)
    monkeypatch.setattr(snapshots_module, "verify_database_file", counted_verify)

    status = await rpc_result(state, "data_store.status")
    created = await rpc_result(state, "data_store.snapshot_create", reason="manual")
    write_incident(
        tmp_path,
        "sessions",
        cause="test-corruption",
        quarantine_path=tmp_path / "quarantine" / "sessions" / "bundle",
        restored_snapshot_id="snapshot-1",
        restored_snapshot_time="2026-08-31T10:00:00.000000Z",
        failure_detected_at="2026-08-31T10:05:00.000000Z",
    )
    incident_status = await rpc_result(state, "data_store.status")
    [incident] = incident_status["incidents"]
    stale = await rpc_error(state, "data_store.incident_acknowledge", incident_id="stale")
    acknowledged = await rpc_result(
        state, "data_store.incident_acknowledge", incident_id=incident["incident_id"]
    )

    assert status["state"] == "snapshot_degraded"
    assert set(status["databases"]) == {"sessions"}
    assert status["databases"]["sessions"]["state"] == "healthy"
    assert created["snapshot"]["reason"] == "manual"
    assert set(created["snapshot"]["members"]) == {"sessions"}
    # The reply reads the fresh snapshot back without re-verifying its directory:
    # the one member is hashed and verified once, while it is created.
    assert (len(hashed), len(verified)) == (1, 1)
    assert incident["database"] == "sessions"
    assert incident_status["state"] == "recovered_with_incident"
    assert stale["code"] == "invalid_request"
    assert acknowledged["state"] == "healthy"
    assert acknowledged["incidents"] == []
    assert resource_changes(state) == [_DATA_STORE_CHANGED, _DATA_STORE_CHANGED]


@pytest.mark.asyncio
async def test_snapshot_create_names_why_no_snapshot_was_taken(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    _register_extension_database(tmp_path).unlink()

    error = await rpc_error(
        _state(tmp_path, sessions), "data_store.snapshot_create", reason="manual"
    )

    assert error["code"] == "domain_error"
    assert error["message"].startswith(
        "the data snapshot was not created: data snapshots need every registered database: "
        f"the registered Extension database {_EXTENSION} has no file"
    )
    assert f"vbot data-store unregister {_EXTENSION} --yes" in error["message"]


@pytest.mark.asyncio
async def test_unregister_releases_an_extension_database(
    tmp_path: Path, sessions: ChatSessionManager
) -> None:
    path = _register_extension_database(tmp_path)
    state = _state(tmp_path, sessions)

    result = await rpc_result(state, "data_store.unregister", name=_EXTENSION)

    released = result["unregistered"]
    assert released["name"] == _EXTENSION
    assert released["database_id"]
    assert (Path(released["quarantine"]) / path.name).is_file()
    assert not path.exists()
    marker = read_marker(tmp_path)
    assert marker is not None
    assert set(marker.databases) == {"sessions"}
    assert resource_changes(state) == [_DATA_STORE_CHANGED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "message"),
    [
        (
            "data_store.unregister",
            {"name": "sessions"},
            "sessions is a core vBot database and cannot be unregistered",
        ),
        (
            "data_store.unregister",
            {"name": "ext.demo.other"},
            "ext.demo.other is not a registered database",
        ),
        ("data_store.unregister", {}, "params.name must be a non-empty string"),
        ("data_store.unregister", {"name": _EXTENSION, "force": True}, "force"),
        ("data_store.snapshot_create", {"reason": "nightly"}, "params.reason must be one of"),
    ],
)
async def test_data_store_refusals_keep_every_database_registered(
    tmp_path: Path,
    sessions: ChatSessionManager,
    method: str,
    params: dict[str, Any],
    message: str,
) -> None:
    path = _register_extension_database(tmp_path)
    state = _state(tmp_path, sessions)

    error = await rpc_error(state, method, **params)

    assert error["code"] == "invalid_request"
    assert message in error["message"]
    marker = read_marker(tmp_path)
    assert marker is not None
    assert set(marker.databases) == {"sessions", _EXTENSION}
    assert path.is_file()
    assert resource_changes(state) == []
