"""CLI contracts for current-format SQLite Session-store operations."""

from __future__ import annotations

from pathlib import Path

from cli import session_store_management
from cli.main import dispatch_session_store_command
from cli.parser import parse_args
from cli.server_management import CommandResult, ServerInstance


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
