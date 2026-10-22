"""CLI contracts for data-store operations on the canonical databases and configuration backups."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import data_store_management
from cli.main import dispatch_data_store_command
from cli.parser import parse_args
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance, ServerState
from core.chat import ChatMessage
from core.database import (
    SnapshotRestore,
    capture_config_backup,
    create_data_snapshot,
    open_database,
    read_marker,
    write_bootstrap_marker,
)
from core.database.marker import begin_maintenance, read_maintenance
from core.database.recovery import incident_path
from core.secrets import CREDENTIALS, KeyFileProtection, SecretStore
from core.sessions import ChatSessionManager, SessionAddress
from tests.cli.cli_test_support import FakeRpc, RunCli
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
        data_store_management.data_store_config_backup_list(instance),
        data_store_management.data_store_config_backup_show(instance, "backup"),
        data_store_management.data_store_config_backup_restore(
            instance, "backup", True, complete=True
        ),
    ):
        assert not result.ok
        assert "local data directory" in result.message


def _recorded(
    calls: list[tuple[str, object]], name: str, detail: object, instance: ServerInstance
) -> CommandResult:
    calls.append((name, detail))
    return CommandResult(ok=True, message=name, instance=instance)


def test_dispatch_routes_nested_data_store_commands(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    calls: list[tuple[str, object]] = []

    def status(resolved: ServerInstance, *, verify_search: bool = False) -> CommandResult:
        calls.append(("status", (resolved, verify_search)))
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
    config_args = [
        parse_args(["data-store", "config-backup", "list"]),
        parse_args(["data-store", "config-backup", "show", "b-1"]),
        parse_args(
            [
                "data-store",
                "config-backup",
                "restore",
                "b-1",
                "--file",
                "settings.json",
                "--file",
                "secrets.json",
                "--yes",
            ]
        ),
        parse_args(["data-store", "config-backup", "restore", "b-1", "--all"]),
    ]

    def config_list(resolved: ServerInstance) -> CommandResult:
        calls.append(("config list", None))
        return CommandResult(ok=True, message="list", instance=resolved)

    def config_show(resolved: ServerInstance, backup_id: str) -> CommandResult:
        calls.append(("config show", backup_id))
        return CommandResult(ok=True, message="show", instance=resolved)

    def config_restore(
        resolved: ServerInstance,
        backup_id: str,
        confirm: bool,
        files: list[str],
        *,
        complete: bool,
    ) -> CommandResult:
        calls.append(("config restore", (backup_id, confirm, tuple(files), complete)))
        return CommandResult(ok=True, message="restore", instance=resolved)

    assert dispatch_data_store_command(status_args, instance, status_fn=status).ok
    assert dispatch_data_store_command(create_args, instance, snapshot_create_fn=create).ok
    for args in (restore_args, documents_args, complete_args):
        assert dispatch_data_store_command(args, instance, snapshot_restore_fn=restore).ok
    assert dispatch_data_store_command(unregister_args, instance, unregister_fn=unregister).ok
    for args in config_args:
        assert dispatch_data_store_command(
            args,
            instance,
            config_backup_list_fn=config_list,
            config_backup_show_fn=config_show,
            config_backup_restore_fn=config_restore,
        ).ok
    prompts: list[str] = []

    def read_secret(prompt: str) -> str:
        prompts.append(prompt)
        return "piped-value"

    for args in (
        parse_args(["data-store", "secrets", "status"]),
        parse_args(["data-store", "secrets", "convert", "--yes"]),
        parse_args(["data-store", "secrets", "reset"]),
        parse_args(["data-store", "secrets", "list"]),
        parse_args(["data-store", "secrets", "set", "GITHUB_TOKEN"]),
        parse_args(["data-store", "secrets", "unset", "GITHUB_TOKEN"]),
    ):
        assert dispatch_data_store_command(
            args,
            instance,
            secrets_status_fn=lambda resolved: _recorded(calls, "secrets status", None, resolved),
            secrets_convert_fn=lambda resolved, confirm: _recorded(
                calls, "secrets convert", confirm, resolved
            ),
            secrets_reset_fn=lambda resolved, confirm: _recorded(
                calls, "secrets reset", confirm, resolved
            ),
            secrets_list_fn=lambda resolved: _recorded(calls, "secrets list", None, resolved),
            secrets_set_fn=lambda resolved, name, value: _recorded(
                calls, "secrets set", (name, value), resolved
            ),
            secrets_unset_fn=lambda resolved, name: _recorded(
                calls, "secrets unset", name, resolved
            ),
            read_secret_fn=read_secret,
        ).ok
    # The value is never an argument: only a prompt or stdin supplies it.
    with pytest.raises(SystemExit):
        parse_args(["data-store", "secrets", "set", "GITHUB_TOKEN", "ghp-secret"])
    assert prompts == ["Value of GITHUB_TOKEN (not shown): "]
    assert calls == [
        ("status", (instance, False)),
        ("create", "update"),
        ("restore", ("s-1", True, ("sessions",), False, False)),
        ("restore", ("s-1", False, (), True, False)),
        ("restore", ("s-1", True, (), False, True)),
        ("unregister", (_EXTENSION, True)),
        ("config list", None),
        ("config show", "b-1"),
        ("config restore", ("b-1", True, ("settings.json", "secrets.json"), False)),
        ("config restore", ("b-1", False, (), True)),
        ("secrets status", None),
        ("secrets convert", True),
        ("secrets reset", False),
        ("secrets list", None),
        ("secrets set", ("GITHUB_TOKEN", "piped-value")),
        ("secrets unset", "GITHUB_TOKEN"),
    ]


@pytest.mark.parametrize("verify_search", [False, True])
def test_status_rpc_is_rendered_without_database_content(
    rpc: FakeRpc, run_cli: RunCli, verify_search: bool
) -> None:
    payload = {"state": "healthy", "databases": {"sessions": {"database_id": "db-1"}}}
    rpc.reply("data_store.status", payload)

    code, output, _stderr = run_cli(
        "data-store", "status", *(["--verify-search"] if verify_search else []), "--output", "plain"
    )

    assert code == 0
    assert json.loads(output) == payload
    assert rpc.calls == [("data_store.status", {"verify_search": True} if verify_search else {})]


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


def test_local_search_verification_is_opt_in_and_read_only(tmp_path: Path, monkeypatch) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    try:
        sessions.create("agent", session_id="check").append(ChatMessage.user("stored history"))
    finally:
        sessions.close()
    database_path = tmp_path / "sessions.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO entries_fts(entries_fts) VALUES('delete-all')")
    before = database_path.read_bytes()
    _stopped_server(monkeypatch, instance)

    quick = json.loads(data_store_management.data_store_status(instance).message)
    verified = json.loads(
        data_store_management.data_store_status(instance, verify_search=True).message
    )

    assert quick["source"] == verified["source"] == "local"
    assert quick["databases"]["sessions"]["state"] == "healthy"
    assert verified["databases"]["sessions"]["state"] == "degraded"
    assert database_path.read_bytes() == before


# A busy server runs too: it must be started again, never left stopped. The server
# of a packaged installation stops and starts through that installation, so a
# systemd unit or native host keeps owning it.
@pytest.mark.parametrize("state", ["running", "unresponsive"], ids=["answering", "busy"])
@pytest.mark.parametrize("packaged", [False, True], ids=["checkout", "installation"])
def test_restore_stops_verifies_and_restarts_the_previously_running_server(
    tmp_path: Path, monkeypatch, state: ServerState, packaged: bool
) -> None:
    from cli.application import processes as application_processes
    from cli.application.state import Installation

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    instance = _instance(data_dir)
    snapshot = _snapshot_with_one_session(data_dir)
    sessions = ChatSessionManager(data_dir)
    sessions.create("agent", session_id="later").append(ChatMessage.user("after the snapshot"))
    sessions.close()
    calls: list[str] = []
    install = Installation(tmp_path / "app", "server", "127.0.0.1", 8420, str(data_dir))
    install.root.mkdir()

    def record(action: str) -> Callable[[object], CommandResult]:
        def run(_target: object) -> CommandResult:
            calls.append(action)
            return CommandResult(ok=True, message=action, instance=instance)

        return run

    _classified(monkeypatch, state, "absent")
    monkeypatch.setattr(
        application_processes, "owning_installation", lambda _target: install if packaged else None
    )
    prefix = "installation " if packaged else ""
    monkeypatch.setattr(data_store_management, "stop_server", record("stop"))
    monkeypatch.setattr(data_store_management, "start_server", record("start"))
    monkeypatch.setattr(application_processes, "stop", record("installation stop"))
    monkeypatch.setattr(application_processes, "start", record("installation start"))

    result = data_store_management.data_store_snapshot_restore(instance, snapshot.name, True)

    assert result.ok is True, result.message
    assert calls == [f"{prefix}stop", f"{prefix}start"]
    assert "(sessions)" in result.message
    assert "restarted the server" in result.message
    restored = ChatSessionManager(data_dir)
    try:
        assert restored.exists(SessionAddress(None, "agent", "restore"))
        assert not restored.exists(SessionAddress(None, "agent", "later"))
    finally:
        restored.close()


@pytest.mark.parametrize("taken", ["before-stop", "after-stop"])
def test_restore_reports_how_it_left_data_and_server_when_another_operation_holds_the_lock(
    tmp_path: Path, monkeypatch, taken: str
) -> None:
    # An update or tray action of the installation can hold its operation lock; the
    # restore then says whether the data changed and that the server stays stopped.
    from cli.application import processes as application_processes
    from cli.application.state import Installation, exclusive

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    instance = _instance(data_dir)
    snapshot = _snapshot_with_one_session(data_dir)
    sessions = ChatSessionManager(data_dir)
    sessions.create("agent", session_id="later").append(ChatMessage.user("after the snapshot"))
    sessions.close()
    install = Installation(tmp_path / "app", "server", "127.0.0.1", 8420, str(data_dir))
    install.root.mkdir()
    other_operation = ExitStack()
    if taken == "before-stop":
        other_operation.enter_context(exclusive(install.root))
    classified: list[ServerState] = []

    def classify(_instance: object, **_kwargs: object) -> ServerState:
        # Running before the restore, stopped once the stop returned.
        classified.append("absent" if classified else "running")
        if taken == "after-stop" and len(classified) == 2:
            other_operation.enter_context(exclusive(install.root))
        return classified[-1]

    def stop(target: Installation) -> CommandResult:
        stopped.append(target)
        return CommandResult(ok=True, message="stopped", instance=instance)

    stopped: list[Installation] = []
    monkeypatch.setattr(
        data_store_management,
        "probe_health_patiently",
        lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
    )
    monkeypatch.setattr(data_store_management, "classify_server", classify)
    monkeypatch.setattr(application_processes, "owning_installation", lambda _target: install)
    monkeypatch.setattr(application_processes, "stop", stop)
    monkeypatch.setattr(
        application_processes, "start", lambda _target: pytest.fail("the lock is held")
    )

    with other_operation:
        result = data_store_management.data_store_snapshot_restore(instance, snapshot.name, True)

    assert result.ok is False
    assert result.message.startswith(
        f"restored data snapshot {snapshot.name}" if taken == "after-stop" else "could not stop"
    )
    assert stopped == ([install] if taken == "after-stop" else [])
    restored = ChatSessionManager(data_dir)
    try:
        assert restored.exists(SessionAddress(None, "agent", "later")) is (taken == "before-stop")
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


def _settings(timezone: str) -> str:
    return json.dumps({"format_version": 1, "timezone": timezone}) + "\n"


def test_config_backup_restore_stops_restores_and_restarts_the_running_server(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(_settings("Europe/Berlin"), encoding="utf-8")
    backup = capture_config_backup(tmp_path, reason="start")
    assert backup is not None
    settings.write_text(_settings("UTC"), encoding="utf-8")
    calls: list[str] = []

    def record(action: str) -> Callable[[ServerInstance], CommandResult]:
        def run(resolved: ServerInstance) -> CommandResult:
            calls.append(action)
            return CommandResult(ok=True, message=action, instance=resolved)

        return run

    _classified(monkeypatch, "running", "absent")
    monkeypatch.setattr(data_store_management, "stop_server", record("stop"))
    monkeypatch.setattr(data_store_management, "start_server", record("start"))

    listed = json.loads(data_store_management.data_store_config_backup_list(instance).message)
    assert [item["backup_id"] for item in listed["config_backups"]] == [backup.backup_id]
    assert listed["config_backups"][0]["changed"] == ["settings.json"]
    shown = json.loads(
        data_store_management.data_store_config_backup_show(instance, backup.backup_id).message
    )
    assert shown["files"]["settings.json"]["state"] == "differs"

    result = data_store_management.data_store_config_backup_restore(
        instance, backup.backup_id, True, ["settings.json"]
    )

    assert result.ok, result.message
    assert calls == ["stop", "start"]
    assert settings.read_text(encoding="utf-8") == _settings("Europe/Berlin")
    newest = json.loads(data_store_management.data_store_config_backup_list(instance).message)
    before = newest["config_backups"][0]
    assert before["reason"] == f"before restore {backup.backup_id}"
    assert before["backup_id"] in result.message
    assert "restarted the server" in result.message

    # An interrupted restore that wrote every file still finishes when it is repeated.
    begin_maintenance(tmp_path, f"config restore {backup.backup_id}")
    _classified(monkeypatch, "absent")

    repeated = data_store_management.data_store_config_backup_restore(
        instance, backup.backup_id, True, ["settings.json"]
    )

    assert repeated.ok, repeated.message
    assert "interrupted restore of this backup is finished" in repeated.message
    assert calls == ["stop", "start", "stop"]
    assert read_maintenance(tmp_path) is None


@pytest.mark.parametrize(
    ("files", "complete", "confirm", "expected"),
    [
        ([], False, True, "--file"),
        (["settings.json"], True, True, "--file"),
        (["settings.json"], False, False, "--yes"),
        (["cron/jobs.json"], False, True, "cannot be restored"),
        (["settings.json"], False, True, "nothing to restore"),
    ],
    ids=["no selector", "both selectors", "unconfirmed", "not in backup", "already equal"],
)
def test_config_backup_restore_refuses_or_finishes_before_stopping_the_server(
    tmp_path: Path,
    monkeypatch,
    files: list[str],
    complete: bool,
    confirm: bool,
    expected: str,
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    (tmp_path / "settings.json").write_text(_settings("UTC"), encoding="utf-8")
    backup = capture_config_backup(tmp_path, reason="start")
    assert backup is not None
    monkeypatch.setattr(
        data_store_management,
        "stop_server",
        lambda *_args: pytest.fail("this restore must not stop the server"),
    )

    result = data_store_management.data_store_config_backup_restore(
        instance, backup.backup_id, confirm, files, complete=complete
    )

    assert result.ok is (expected == "nothing to restore")
    assert expected in result.message


def test_secrets_convert_runs_while_the_server_is_stopped_and_reset_spares_an_open_store(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-openai\n", encoding="utf-8")
    calls: list[str] = []

    def record(action: str) -> Callable[[ServerInstance], CommandResult]:
        def run(resolved: ServerInstance) -> CommandResult:
            calls.append(action)
            return CommandResult(ok=True, message=action, instance=resolved)

        return run

    _classified(monkeypatch, "running", "absent")
    monkeypatch.setattr(data_store_management, "stop_server", record("stop"))
    monkeypatch.setattr(data_store_management, "start_server", record("start"))

    unconfirmed = data_store_management.data_store_secrets_convert(instance, False)
    status = data_store_management.data_store_secrets_status(instance)

    assert not unconfirmed.ok and "--yes" in unconfirmed.message
    assert json.loads(status.message)["plaintext_files"] == [".env"]
    assert "sk-openai" not in status.message
    assert calls == []

    converted = data_store_management.data_store_secrets_convert(instance, True)

    assert converted.ok, converted.message
    assert "imported 1 credentials" in converted.message
    assert "deleted .env" in converted.message
    assert calls == ["stop", "start"]
    assert not env.exists()
    assert SecretStore(tmp_path).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-openai"

    reset = data_store_management.data_store_secrets_reset(instance, True)

    assert not reset.ok and "the Secret Store is available" in reset.message
    assert calls == ["stop", "start"]
    assert SecretStore(tmp_path).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-openai"


class _StatusPayload:
    ok = True

    def __init__(self, secrets: dict[str, object]) -> None:
        self.data = {"state": "ok", "secrets": secrets}


@pytest.mark.parametrize(
    ("server", "refusal"),
    [
        ("opens it", "the running server reports the Secret Store available"),
        ("searches elsewhere", "the running server would not find the key"),
        ("on another port", "a vBot server is running on the data directory (port 8421)"),
        ("cannot open it either", None),
        ("stopped", None),
    ],
)
def test_secrets_reset_replaces_only_a_store_the_server_cannot_open_either(
    tmp_path: Path, monkeypatch, server: str, refusal: str | None
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    # The server's key lies where this command's environment does not look.
    server_keys = tmp_path / "server-keys"
    server_store = SecretStore(
        tmp_path, protections=(KeyFileProtection(directories=(server_keys,)),)
    )
    server_store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")
    local = SecretStore(tmp_path).status()
    assert local.state == "unavailable"
    document = (tmp_path / "secrets.json").read_bytes()
    server_status: dict[str, object] = {
        "state": "available" if server == "opens it" else "unavailable",
        "key_locations": [str(server_keys / f"{local.store_id}.key")],
        "new_key_location": (
            local.new_key_location if server == "cannot open it either" else str(server_keys)
        ),
    }
    running = server in {"opens it", "searches elsewhere", "cannot open it either"}
    calls: list[str] = []

    def record(action: str) -> Callable[[ServerInstance], CommandResult]:
        def run(resolved: ServerInstance) -> CommandResult:
            calls.append(action)
            return CommandResult(ok=True, message=action, instance=resolved)

        return run

    monkeypatch.setattr(
        data_store_management,
        "rpc_call",
        lambda *_args: (
            _StatusPayload(server_status) if running else _FailedPayload(instance, "refused")
        ),
    )
    monkeypatch.setattr(
        data_store_management,
        "probe_health",
        lambda _instance: HealthProbeResult(reachable=running, is_vbot=running),
    )
    monkeypatch.setattr(
        data_store_management,
        "live_server_ports",
        lambda _data_dir: (8421,) if server == "on another port" else (),
    )
    _classified(monkeypatch, "running" if running else "absent", "absent")
    monkeypatch.setattr(data_store_management, "stop_server", record("stop"))
    monkeypatch.setattr(data_store_management, "start_server", record("start"))

    result = data_store_management.data_store_secrets_reset(instance, True)

    if refusal is not None:
        assert not result.ok and refusal in result.message
        if running:
            # Both lookups are named, so the operator sees why the two differ.
            assert local.key_locations[0] in result.message
            assert str(server_keys) in result.message
        assert calls == []
        assert (tmp_path / "secrets.json").read_bytes() == document
        return
    assert result.ok, result.message
    assert calls == (["stop", "start"] if running else ["stop"])
    replaced = SecretStore(tmp_path).status()
    assert replaced.state == "available" and replaced.credentials == 0
    assert f"the new key is at {replaced.key_location}" in result.message
    [kept] = (tmp_path / "quarantine" / "secret-store").iterdir()
    old = SecretStore(kept, protections=(KeyFileProtection(directories=(server_keys,)),))
    assert old.entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-openai"


@pytest.mark.parametrize("terminal", [True, False])
def test_a_secret_value_comes_from_a_hidden_prompt_or_from_piped_stdin(
    monkeypatch, terminal: bool
) -> None:
    import getpass
    import io
    import sys

    from cli._input import _read_secret

    stdin = SimpleNamespace(isatty=lambda: terminal, buffer=io.BytesIO(b"ghp-piped\r\n"))
    monkeypatch.setattr(sys, "stdin", stdin)
    prompts: list[str] = []

    def hidden(prompt: str) -> str:
        prompts.append(prompt)
        return "ghp-typed"

    monkeypatch.setattr(getpass, "getpass", hidden)

    value = _read_secret("Value: ")

    assert value == ("ghp-typed" if terminal else "ghp-piped")
    assert prompts == (["Value: "] if terminal else [])


class _Answered:
    ok = True

    def __init__(self, data: dict[str, object]) -> None:
        self.data = data


def test_secrets_set_and_unset_go_through_the_server_or_write_locally_while_none_runs(
    tmp_path: Path, monkeypatch
) -> None:
    instance = _instance(tmp_path)
    write_bootstrap_marker(tmp_path)
    requests: list[tuple[str, dict]] = []

    def answered(_instance, method: str, params: dict) -> _Answered:
        requests.append((method, params))
        return _Answered({"name": params["name"], "changed": True, "source": "data_dir"})

    monkeypatch.setattr(data_store_management, "rpc_call", answered)
    through_server = data_store_management.data_store_secrets_set(instance, "GH_TOKEN", "ghp-x")

    assert through_server.ok and "ghp-x" not in through_server.message
    assert requests == [("secrets.set", {"name": "GH_TOKEN", "value": "ghp-x"})]
    assert not (tmp_path / "secrets.json").exists()
    # Refused before anything is sent; a mistyped name is never repeated.
    for name, value, problem in (
        ("ghp-pasted-into-the-name", "x", "environment variable name"),
        ("GH_TOKEN", "", "no value was given"),
        ("GH_TOKEN", "ghp-a\nghp-b", "single line"),
    ):
        refused = data_store_management.data_store_secrets_set(instance, name, value)
        assert not refused.ok and problem in refused.message
        assert "ghp-" not in refused.message
    assert len(requests) == 1

    _stopped_server(monkeypatch, instance)
    monkeypatch.setattr(data_store_management, "live_server_ports", lambda _data_dir: ())
    saved = data_store_management.data_store_secrets_set(instance, "GH_TOKEN", "ghp-x")
    listed = data_store_management.data_store_secrets_list(instance)

    assert saved.ok, saved.message
    assert json.loads(saved.message) == {"name": "GH_TOKEN", "changed": True, "local": True}
    assert SecretStore(tmp_path).entry(CREDENTIALS, "GH_TOKEN") == "ghp-x"
    assert json.loads(listed.message)["credentials"] == [
        {"name": "GH_TOKEN", "configured": True, "source": "data_dir", "damaged": False}
    ]
    assert "ghp-x" not in listed.message

    # Another server runs on the data directory: nothing changes locally.
    monkeypatch.setattr(data_store_management, "live_server_ports", lambda _data_dir: (8421,))
    refused = data_store_management.data_store_secrets_unset(instance, "GH_TOKEN")
    assert not refused.ok and "port 8421" in refused.message
    assert SecretStore(tmp_path).entry(CREDENTIALS, "GH_TOKEN") == "ghp-x"

    monkeypatch.setattr(data_store_management, "live_server_ports", lambda _data_dir: ())
    removed = data_store_management.data_store_secrets_unset(instance, "GH_TOKEN")
    assert json.loads(removed.message) == {"name": "GH_TOKEN", "removed": True, "local": True}
    assert SecretStore(tmp_path).entry_names(CREDENTIALS) == ()
