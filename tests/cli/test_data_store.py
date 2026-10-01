"""CLI contracts for data-store operations on the canonical SQLite databases."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from cli import data_store_management
from cli.main import dispatch_data_store_command
from cli.parser import parse_args
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance, ServerState
from core.chat import ChatMessage
from core.database import (
    SnapshotRestore,
    create_data_snapshot,
    open_database,
    read_marker,
    write_bootstrap_marker,
)
from core.database.recovery import incident_path
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.database.database_test_support import notes_spec

_EXTENSION = "ext.demo.notes"


def _instance(tmp_path: Path) -> ServerInstance:
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=tmp_path,
        url="http://127.0.0.1:8420",
        log_path=tmp_path / "server.log",
    )


def _snapshot_with_one_session(tmp_path: Path) -> Path:
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    try:
        sessions.create("agent", session_id="restore").append(ChatMessage.user("retained"))
        snapshot = create_data_snapshot(tmp_path, reason="test", databases=(sessions.database,))
    finally:
        sessions.close()
    assert snapshot is not None
    return snapshot


class _FailedPayload:
    ok = False

    def __init__(self, instance: ServerInstance, message: str) -> None:
        self._instance = instance
        self._message = message

    def to_command_result(self) -> CommandResult:
        return CommandResult(ok=False, message=self._message, instance=self._instance)


def test_remote_data_store_commands_never_inspect_local_data(tmp_path: Path, monkeypatch) -> None:
    instance = replace(_instance(tmp_path), host="remote.example", url="http://remote.example:8420")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("remote target must not read or restore local state")

    monkeypatch.setattr(
        data_store_management,
        "rpc_call",
        lambda *_args: _FailedPayload(instance, "remote RPC unavailable"),
    )
    monkeypatch.setattr(
        data_store_management,
        "probe_health",
        lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
    )
    monkeypatch.setattr(data_store_management, "_local_status", forbidden)
    monkeypatch.setattr(data_store_management, "read_marker", forbidden)
    monkeypatch.setattr(data_store_management, "restore_data_snapshot", forbidden)
    monkeypatch.setattr(data_store_management, "stop_server", forbidden)
    monkeypatch.setattr(data_store_management, "unregister_database", forbidden)

    for answered_by_rpc in (
        data_store_management.data_store_status(instance),
        data_store_management.data_store_unregister(instance, _EXTENSION, True),
    ):
        assert not answered_by_rpc.ok
        assert answered_by_rpc.message == "remote RPC unavailable"
    for result in (
        data_store_management.data_store_snapshot_list(instance),
        data_store_management.data_store_snapshot_verify(instance, "snapshot"),
        data_store_management.data_store_snapshot_restore(instance, "snapshot", True),
    ):
        assert not result.ok
        assert "local data directory" in result.message


def test_dispatch_routes_nested_data_store_commands(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    calls: list[tuple[str, object]] = []

    def status(resolved: ServerInstance) -> CommandResult:
        calls.append(("status", resolved))
        return CommandResult(ok=True, message="status", instance=resolved)

    def create(resolved: ServerInstance, reason: str) -> CommandResult:
        calls.append(("create", reason))
        return CommandResult(ok=True, message="create", instance=resolved)

    def restore(
        resolved: ServerInstance,
        snapshot_id: str,
        confirm: bool,
        databases: list[str],
        *,
        documents: bool,
        complete: bool,
    ) -> CommandResult:
        calls.append(("restore", (snapshot_id, confirm, tuple(databases), documents, complete)))
        return CommandResult(ok=True, message="restore", instance=resolved)

    def unregister(resolved: ServerInstance, name: str, confirm: bool) -> CommandResult:
        calls.append(("unregister", (name, confirm)))
        return CommandResult(ok=True, message="unregister", instance=resolved)

    status_args = parse_args(["data-store", "status"])
    create_args = parse_args(["data-store", "snapshot", "create", "--reason", "update"])
    restore_args = parse_args(
        ["data-store", "snapshot", "restore", "s-1", "--database", "sessions", "--yes"]
    )
    documents_args = parse_args(["data-store", "snapshot", "restore", "s-1", "--documents"])
    complete_args = parse_args(["data-store", "snapshot", "restore", "s-1", "--all", "--yes"])
    unregister_args = parse_args(["data-store", "unregister", _EXTENSION, "--yes"])

    assert dispatch_data_store_command(status_args, instance, status_fn=status).ok
    assert dispatch_data_store_command(create_args, instance, snapshot_create_fn=create).ok
    for args in (restore_args, documents_args, complete_args):
        assert dispatch_data_store_command(args, instance, snapshot_restore_fn=restore).ok
    assert dispatch_data_store_command(unregister_args, instance, unregister_fn=unregister).ok
    assert calls == [
        ("status", instance),
        ("create", "update"),
        ("restore", ("s-1", True, ("sessions",), False, False)),
        ("restore", ("s-1", False, (), True, False)),
        ("restore", ("s-1", True, (), False, True)),
        ("unregister", (_EXTENSION, True)),
    ]


def test_status_rpc_is_rendered_without_database_content(tmp_path: Path, monkeypatch) -> None:
    instance = _instance(tmp_path)

    class Payload:
        ok = True
        data = {"state": "healthy", "databases": {"sessions": {"database_id": "db-1"}}}

    monkeypatch.setattr(data_store_management, "rpc_call", lambda *_args: Payload())

    result = data_store_management.data_store_status(instance)

    assert result.ok is True
    assert '"database_id": "db-1"' in result.message


def test_verify_reports_the_snapshot_from_its_verified_read(tmp_path: Path, monkeypatch) -> None:
    instance = _instance(tmp_path)
    snapshot = _snapshot_with_one_session(tmp_path)
    # Retention can change a later inventory after the requested image was verified.
    monkeypatch.setattr(data_store_management, "snapshot_summaries", lambda *_a, **_k: [])

    result = data_store_management.data_store_snapshot_verify(instance, snapshot.name)

    assert result.ok
    payload = json.loads(result.message)
    assert payload["snapshot_id"] == snapshot.name
    assert payload["members"]["sessions"]["facts"]["session_count"] == 1


def test_snapshot_list_reports_verified_snapshots(tmp_path: Path) -> None:
    snapshot = _snapshot_with_one_session(tmp_path)

    result = data_store_management.data_store_snapshot_list(_instance(tmp_path))

    assert result.ok
    assert [item["snapshot_id"] for item in json.loads(result.message)["snapshots"]] == [
        snapshot.name
    ]


def _stopped_server(monkeypatch, instance: ServerInstance) -> None:
    monkeypatch.setattr(
        data_store_management,
        "rpc_call",
        lambda *_args: _FailedPayload(instance, "RPC unavailable"),
    )
    monkeypatch.setattr(
        data_store_management,
        "probe_health",
        lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
    )
    _classified(monkeypatch, "absent")


def _classified(monkeypatch, *states: ServerState) -> None:
    """Classify the target as *states* in turn; the last one repeats."""
    remaining = list(states)
    monkeypatch.setattr(
        data_store_management,
        "probe_health_patiently",
        lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
    )
    monkeypatch.setattr(
        data_store_management,
        "classify_server",
        lambda _instance, **_kwargs: remaining.pop(0) if len(remaining) > 1 else remaining[0],
    )


def test_status_falls_back_to_a_local_read_of_a_damaged_database(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    ChatSessionManager(tmp_path).close()
    (tmp_path / "sessions.db").write_bytes(b"damaged")
    _stopped_server(monkeypatch, instance)

    result = data_store_management.data_store_status(instance)

    assert result.ok is False
    payload = json.loads(result.message)
    assert payload["source"] == "local"
    assert payload["state"] == "unavailable"
    assert payload["databases"]["sessions"]["state"] == "unavailable"
    assert payload["snapshots"] == []


def test_local_status_reports_invalid_recovery_evidence_without_changing_it(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    ChatSessionManager(tmp_path).close()
    evidence = incident_path(tmp_path, "sessions")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("[]", encoding="utf-8")
    _stopped_server(monkeypatch, instance)

    result = data_store_management.data_store_status(instance)

    payload = json.loads(result.message)
    assert result.ok is False
    assert payload["state"] == "unavailable"
    assert "recovery incident" in str(payload["reason"])
    assert payload["incidents"] == []
    assert evidence.read_text(encoding="utf-8") == "[]"


# A busy server runs too: it must be started again, never left stopped.
@pytest.mark.parametrize("state", ["running", "unresponsive"], ids=["answering", "busy"])
def test_restore_stops_verifies_and_restarts_the_previously_running_server(
    tmp_path: Path, monkeypatch, state: ServerState
) -> None:
    instance = _instance(tmp_path)
    snapshot = _snapshot_with_one_session(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("agent", session_id="later").append(ChatMessage.user("after the snapshot"))
    sessions.close()
    calls: list[str] = []

    def stop(resolved: ServerInstance) -> CommandResult:
        calls.append("stop")
        return CommandResult(ok=True, message="stopped", instance=resolved)

    def start(resolved: ServerInstance) -> CommandResult:
        calls.append("start")
        return CommandResult(ok=True, message="started", instance=resolved)

    _classified(monkeypatch, state, "absent")
    monkeypatch.setattr(data_store_management, "stop_server", stop)
    monkeypatch.setattr(data_store_management, "start_server", start)

    result = data_store_management.data_store_snapshot_restore(instance, snapshot.name, True)

    assert result.ok is True, result.message
    assert calls == ["stop", "start"]
    assert "(sessions)" in result.message
    assert "restarted the server" in result.message
    restored = ChatSessionManager(tmp_path)
    try:
        assert restored.exists(SessionAddress(None, "agent", "restore"))
        assert not restored.exists(SessionAddress(None, "agent", "later"))
    finally:
        restored.close()


def test_restore_refuses_an_unknown_snapshot_before_stopping_the_server(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    monkeypatch.setattr(
        data_store_management,
        "stop_server",
        lambda *_args: pytest.fail("a restore that cannot succeed must not stop the server"),
    )

    result = data_store_management.data_store_snapshot_restore(instance, "missing", True)

    assert not result.ok
    assert "cannot be restored" in result.message


@pytest.mark.parametrize("stop_succeeds", [True, False])
def test_restore_checks_process_shutdown_even_after_listener_closed(
    tmp_path: Path, monkeypatch, stop_succeeds: bool
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    calls: list[str] = []

    def stop(resolved):
        calls.append("stop")
        return CommandResult(ok=stop_succeeds, message="shutdown result", instance=resolved)

    def restore(*_args, check_only: bool = False, **_kwargs):
        calls.append("check" if check_only else "restore")
        return SnapshotRestore("snapshot")

    _classified(monkeypatch, "absent")
    monkeypatch.setattr(data_store_management, "stop_server", stop)
    monkeypatch.setattr(data_store_management, "restore_data_snapshot", restore)
    monkeypatch.setattr(
        data_store_management,
        "start_server",
        lambda *_args: pytest.fail("an already stopped target must remain stopped"),
    )

    result = data_store_management.data_store_snapshot_restore(instance, "snapshot", True)

    assert result.ok is stop_succeeds
    assert calls == (["check", "stop", "restore"] if stop_succeeds else ["check", "stop"])


def _registered_extension(tmp_path: Path) -> Path:
    """A data directory whose only registered database is ``_EXTENSION``; returns a snapshot."""
    write_bootstrap_marker(tmp_path)
    open_database(notes_spec(tmp_path, name=_EXTENSION)).close()
    snapshot = create_data_snapshot(tmp_path, reason="test")
    assert snapshot is not None
    return snapshot


def _registered_names(tmp_path: Path) -> set[str]:
    marker = read_marker(tmp_path)
    assert marker is not None
    return set(marker.databases)


def test_unregister_requires_confirmation_before_contacting_the_server(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        data_store_management,
        "rpc_call",
        lambda *_args: pytest.fail("an unconfirmed unregister must not contact the server"),
    )

    result = data_store_management.data_store_unregister(_instance(tmp_path), _EXTENSION, False)

    assert not result.ok
    assert "re-run with --yes" in result.message


def test_unregister_is_performed_and_refused_by_a_running_server(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    requests: list[tuple[str, dict]] = []

    class Released:
        ok = True
        data = {"unregistered": {"name": _EXTENSION, "database_id": "db-1", "quarantine": None}}

    def released(_instance, method: str, params: dict):
        requests.append((method, params))
        return Released()

    monkeypatch.setattr(data_store_management, "rpc_call", released)
    result = data_store_management.data_store_unregister(instance, _EXTENSION, True)
    assert result.ok
    assert json.loads(result.message)["unregistered"]["name"] == _EXTENSION
    assert requests == [("data_store.unregister", {"name": _EXTENSION})]

    monkeypatch.setattr(
        data_store_management,
        "rpc_call",
        lambda *_args: _FailedPayload(instance, "the database is open by its Extension"),
    )
    monkeypatch.setattr(
        data_store_management,
        "probe_health",
        lambda _instance: HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
    )
    monkeypatch.setattr(
        data_store_management,
        "unregister_database",
        lambda *_args: pytest.fail("a refusal by the running server must not fall back"),
    )
    refused = data_store_management.data_store_unregister(instance, _EXTENSION, True)
    assert not refused.ok
    assert refused.message == "the database is open by its Extension"


def test_unregister_releases_locally_and_a_restore_registers_it_again(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    snapshot = _registered_extension(tmp_path)
    path = notes_spec(tmp_path, name=_EXTENSION).path
    _stopped_server(monkeypatch, instance)
    monkeypatch.setattr(data_store_management, "live_server_ports", lambda _data_dir: ())
    monkeypatch.setattr(
        data_store_management,
        "stop_server",
        lambda resolved: CommandResult(ok=True, message="stopped", instance=resolved),
    )

    result = data_store_management.data_store_unregister(instance, _EXTENSION, True)

    assert result.ok, result.message
    payload = json.loads(result.message)
    assert payload["source"] == "local"
    assert payload["unregistered"]["name"] == _EXTENSION
    assert (Path(payload["unregistered"]["quarantine"]) / path.name).is_file()
    assert not path.exists()
    assert _registered_names(tmp_path) == set()

    restored = data_store_management.data_store_snapshot_restore(
        instance, snapshot.name, True, [_EXTENSION]
    )

    assert restored.ok, restored.message
    assert f"registered again: {_EXTENSION}" in restored.message
    assert path.is_file()
    assert _registered_names(tmp_path) == {_EXTENSION}


def _core_database(tmp_path: Path) -> None:
    write_bootstrap_marker(tmp_path)
    ChatSessionManager(tmp_path).close()


@pytest.mark.parametrize(
    ("prepare", "name", "live_ports", "reason"),
    [
        pytest.param(
            _registered_extension,
            _EXTENSION,
            (8421,),
            "a vBot server is running on the data directory (port 8421)",
            id="server-running-on-the-data-directory",
        ),
        pytest.param(
            _core_database,
            "sessions",
            (),
            "sessions is a core vBot database and cannot be unregistered",
            id="core-database",
        ),
    ],
)
def test_local_unregister_refuses_and_keeps_the_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare: Callable[[Path], object],
    name: str,
    live_ports: tuple[int, ...],
    reason: str,
) -> None:
    instance = _instance(tmp_path)
    prepare(tmp_path)
    _stopped_server(monkeypatch, instance)
    monkeypatch.setattr(data_store_management, "live_server_ports", lambda _data_dir: live_ports)

    result = data_store_management.data_store_unregister(instance, name, True)

    assert not result.ok
    assert reason in result.message
    assert _registered_names(tmp_path) == {name}
    if name == _EXTENSION:
        assert notes_spec(tmp_path, name=_EXTENSION).path.is_file()
