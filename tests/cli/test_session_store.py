"""CLI contracts for current-format SQLite Session-store operations."""

from __future__ import annotations

from pathlib import Path

from cli import session_store_management
from cli.main import dispatch_session_store_command
from cli.parser import parse_args
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance
from core.chat import ChatMessage
from core.sessions import ChatSessionManager
from core.sessions.format import read_session_store_marker, write_bootstrap_marker
from core.sessions.snapshots import create_snapshot


def _instance(tmp_path: Path) -> ServerInstance:
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=tmp_path,
        url="http://127.0.0.1:8420",
        log_path=tmp_path / "server.log",
    )


def test_dispatch_routes_nested_session_store_commands(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    calls: list[tuple[str, object]] = []

    def status(resolved: ServerInstance) -> CommandResult:
        calls.append(("status", resolved))
        return CommandResult(ok=True, message="status", instance=resolved)

    def create(resolved: ServerInstance, reason: str) -> CommandResult:
        calls.append(("create", reason))
        return CommandResult(ok=True, message="create", instance=resolved)

    status_args = parse_args(["session-store", "status"])
    create_args = parse_args(["session-store", "snapshot", "create", "--reason", "update"])

    assert dispatch_session_store_command(status_args, instance, status_fn=status).ok
    assert dispatch_session_store_command(
        create_args,
        instance,
        snapshot_create_fn=create,
    ).ok
    assert calls == [("status", instance), ("create", "update")]


def test_snapshot_status_rpc_is_rendered_without_session_content(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)

    class Payload:
        ok = True
        data = {"state": "ready", "database_id": "db-1"}

    monkeypatch.setattr(session_store_management, "rpc_call", lambda *_args: Payload())

    result = session_store_management.session_store_status(instance)

    assert result.ok is True
    assert '"database_id": "db-1"' in result.message
    assert "sessions" not in result.message


def test_status_falls_back_to_offline_unrecoverable_projection(tmp_path: Path, monkeypatch) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.close()
    (tmp_path / "sessions.db").write_bytes(b"damaged")

    class Payload:
        ok = False

        @staticmethod
        def to_command_result() -> CommandResult:
            return CommandResult(ok=False, message="RPC unavailable", instance=instance)

    monkeypatch.setattr(session_store_management, "rpc_call", lambda *_args: Payload())
    monkeypatch.setattr(
        session_store_management,
        "probe_health",
        lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
    )

    result = session_store_management.session_store_status(instance)

    assert result.ok is False
    assert '"state": "unrecoverable"' in result.message
    assert '"snapshots": []' in result.message


def test_restore_stops_verifies_and_restarts_the_previously_running_server(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("agent", session_id="restore").append(ChatMessage.user("retained"))
    marker = read_session_store_marker(tmp_path)
    assert marker is not None
    snapshot = create_snapshot(
        tmp_path,
        tmp_path / "sessions.db",
        sessions.backup_snapshot,
        database_id=str(marker["database_id"]),
        reason="test",
    )
    sessions.close()
    assert snapshot is not None
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

    monkeypatch.setattr(session_store_management, "probe_health", lambda _instance: next(probes))
    monkeypatch.setattr(session_store_management, "is_systemd_managed", lambda *_args: False)
    monkeypatch.setattr(session_store_management, "stop_server", stop)
    monkeypatch.setattr(session_store_management, "start_server", start)

    result = session_store_management.session_store_snapshot_restore(instance, snapshot.name, True)

    assert result.ok is True
    assert calls == ["stop", "start"]
    assert "restarted the server" in result.message
