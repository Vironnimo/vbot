"""CLI contracts for data-store operations on the canonical SQLite databases."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cli import data_store_management
from cli.main import dispatch_data_store_command
from cli.parser import parse_args
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance
from core.chat import ChatMessage
from core.database import SnapshotRestore, create_data_snapshot, write_bootstrap_marker
from core.database.recovery import incident_path
from core.sessions import ChatSessionManager, SessionAddress


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

    status = data_store_management.data_store_status(instance)
    assert not status.ok
    assert status.message == "remote RPC unavailable"
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

    status_args = parse_args(["data-store", "status"])
    create_args = parse_args(["data-store", "snapshot", "create", "--reason", "update"])
    restore_args = parse_args(
        ["data-store", "snapshot", "restore", "s-1", "--database", "sessions", "--yes"]
    )
    documents_args = parse_args(["data-store", "snapshot", "restore", "s-1", "--documents"])
    complete_args = parse_args(["data-store", "snapshot", "restore", "s-1", "--all", "--yes"])

    assert dispatch_data_store_command(status_args, instance, status_fn=status).ok
    assert dispatch_data_store_command(create_args, instance, snapshot_create_fn=create).ok
    for args in (restore_args, documents_args, complete_args):
        assert dispatch_data_store_command(args, instance, snapshot_restore_fn=restore).ok
    assert calls == [
        ("status", instance),
        ("create", "update"),
        ("restore", ("s-1", True, ("sessions",), False, False)),
        ("restore", ("s-1", False, (), True, False)),
        ("restore", ("s-1", True, (), False, True)),
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


def test_restore_stops_verifies_and_restarts_the_previously_running_server(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    snapshot = _snapshot_with_one_session(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("agent", session_id="later").append(ChatMessage.user("after the snapshot"))
    sessions.close()
    probes = iter(
        (
            HealthProbeResult(reachable=True, is_vbot=True, status_code=200),
            HealthProbeResult(reachable=False, is_vbot=False),
        )
    )
    calls: list[str] = []

    def stop(resolved: ServerInstance) -> CommandResult:
        calls.append("stop")
        return CommandResult(ok=True, message="stopped", instance=resolved)

    def start(resolved: ServerInstance) -> CommandResult:
        calls.append("start")
        return CommandResult(ok=True, message="started", instance=resolved)

    monkeypatch.setattr(data_store_management, "probe_health", lambda _instance: next(probes))
    monkeypatch.setattr(data_store_management, "is_systemd_managed", lambda *_args: False)
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

    monkeypatch.setattr(
        data_store_management,
        "probe_health",
        lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
    )
    monkeypatch.setattr(data_store_management, "is_systemd_managed", lambda *_args: False)
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
