"""Tests for the ``vbot`` entry point: local commands, server lifecycle and update output."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from cli import _commands
from cli import main as cli_main
from cli.server_management import (
    UNRESPONSIVE_LISTENER_MESSAGE,
    CommandResult,
    HealthProbeResult,
    ServerInstance,
    WebUIProbeResult,
)
from core.utils.config import VBOT_ROOT
from tests.cli.cli_test_support import make_instance

TARGET_FLAGS = ("--host", "localhost", "--port", "8765", "--data-dir", "data")
FLAGGED_TARGET = {"host": "localhost", "port": 8765, "data_dir": "data"}
DEFAULT_TARGET = {"host": "127.0.0.1", "port": None, "data_dir": None}
CONFLICT = "port occupied by non-vBot process"
VBOT_HEALTH = HealthProbeResult(reachable=True, is_vbot=True, status_code=200)
FOREIGN_HEALTH = HealthProbeResult(reachable=True, is_vbot=False, status_code=200)


def test_home_prints_app_and_data_directories_without_a_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_resolve(**kwargs: object) -> ServerInstance:
        raise AssertionError(f"home must not resolve a server: {kwargs}")

    exit_code = cli_main.run(["home", "--data-dir", "runtime-data"], resolve=fail_resolve)

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        f"vbot_root: {VBOT_ROOT}",
        f"data_dir: {tmp_path / 'runtime-data'}",
    ]


@pytest.mark.parametrize(
    "argv",
    [["update"], ["uninstall", "--all", "--yes"], ["autostart", "enable"]],
    ids=lambda argv: argv[0],
)
def test_a_development_checkout_refuses_installation_lifecycle_commands(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_resolve(**kwargs: object) -> ServerInstance:
        raise AssertionError(f"a development checkout has no installation: {kwargs}")

    exit_code = cli_main.run(argv, resolve=fail_resolve)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "development checkout" in captured.out + captured.err


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(("--host", "192.168.1.50", "--port", "8500"), id="explicit-target"),
        pytest.param((), id="recorded-target"),
    ],
)
def test_desktop_forwards_only_the_supplied_target_flags(
    capsys: pytest.CaptureFixture[str], flags: tuple[str, ...]
) -> None:
    launches: list[list[str]] = []

    def launch(launch_argv: Sequence[str]) -> None:
        launches.append(list(launch_argv))

    exit_code = cli_main.run(["desktop", *flags], launch_desktop_fn=launch)

    assert exit_code == 0
    assert launches == [list(flags)]
    assert capsys.readouterr().out.strip()


def test_desktop_reports_a_launcher_failure(capsys: pytest.CaptureFixture[str]) -> None:
    def launch(launch_argv: Sequence[str]) -> None:
        raise RuntimeError("pywebview is required to run vBot Desktop")

    exit_code = cli_main.run(["desktop"], launch_desktop_fn=launch)

    assert exit_code == 1
    output = capsys.readouterr().out
    assert output.startswith("error:")
    assert "pywebview is required to run vBot Desktop" in output


URL = "http://127.0.0.1:8420"


@pytest.mark.parametrize(
    ("command", "flags", "target", "result", "code", "shown", "completion"),
    [
        pytest.param(
            "start",
            TARGET_FLAGS,
            FLAGGED_TARGET,
            {
                "message": "started",
                "health": VBOT_HEALTH,
                "webui": WebUIProbeResult(available=False, status_code=404),
            },
            0,
            ("result: started", "running: yes", "webui: unavailable"),
            f"[WARN] The vBot server started successfully and is healthy at {URL}.",
            id="started-without-webui",
        ),
        pytest.param(
            "start",
            (),
            DEFAULT_TARGET,
            {
                "ok": False,
                "message": "server readiness timed out",
                "health": HealthProbeResult(reachable=False, is_vbot=False, error="ConnectError"),
            },
            1,
            ("result: server readiness timed out", "running: no"),
            f"[ERROR] Could not start the vBot server at {URL}: server readiness timed out.",
            id="start-timed-out",
        ),
        pytest.param(
            "start",
            (),
            DEFAULT_TARGET,
            {"ok": False, "message": CONFLICT, "health": FOREIGN_HEALTH},
            1,
            (f"conflict: {CONFLICT}",),
            f"[ERROR] Could not start the vBot server at {URL}: {CONFLICT}.",
            id="start-conflict",
        ),
        pytest.param(
            "stop",
            TARGET_FLAGS,
            FLAGGED_TARGET,
            {"message": "stopped"},
            0,
            ("result: stopped",),
            f"[OK] The vBot server stopped successfully at {URL}.",
            id="stopped",
        ),
        pytest.param(
            "stop",
            (),
            DEFAULT_TARGET,
            {
                "ok": False,
                "message": CONFLICT,
                "health": FOREIGN_HEALTH,
                "process_id": 123,
                "forced": True,
            },
            1,
            ("process_id: 123", "forced: true", f"conflict: {CONFLICT}"),
            f"[ERROR] Could not stop the vBot server at {URL}: {CONFLICT}.",
            id="stop-conflict",
        ),
        pytest.param(
            "status",
            TARGET_FLAGS,
            FLAGGED_TARGET,
            {
                "message": "running",
                "health": VBOT_HEALTH,
                "webui": WebUIProbeResult(available=True, status_code=200),
            },
            0,
            ("running: yes", "webui: available"),
            f"[OK] The vBot server is running and healthy at {URL}.",
            id="status-running",
        ),
        pytest.param(
            "status",
            (),
            DEFAULT_TARGET,
            {"message": "not running"},
            0,
            ("result: not running", "running: no", "webui: unknown"),
            f"[WARN] The vBot server is not running at {URL}.",
            id="status-not-running",
        ),
        pytest.param(
            "status",
            (),
            DEFAULT_TARGET,
            {
                "ok": False,
                "message": CONFLICT,
                "health": FOREIGN_HEALTH,
                "webui": WebUIProbeResult(available=False),
            },
            0,
            ("running: no", "webui: unavailable", f"conflict: {CONFLICT}"),
            f"[ERROR] The vBot server is not running at {URL}; "
            "the port is occupied by a non-vBot process.",
            id="status-conflict-is-an-answer",
        ),
        pytest.param(
            "status",
            (),
            DEFAULT_TARGET,
            {
                "ok": False,
                "message": UNRESPONSIVE_LISTENER_MESSAGE,
                "health": HealthProbeResult(
                    reachable=False, is_vbot=False, timed_out=True, unresponsive=True
                ),
            },
            1,
            (f"result: {UNRESPONSIVE_LISTENER_MESSAGE}", "running: no", "webui: unknown"),
            f"[ERROR] Could not determine the status of the vBot server at {URL}: "
            f"{UNRESPONSIVE_LISTENER_MESSAGE}.",
            id="status-unresponsive-listener-fails",
        ),
    ],
)
def test_server_commands_resolve_the_target_and_print_the_status_frame(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    flags: tuple[str, ...],
    target: dict[str, Any],
    result: dict[str, Any],
    code: int,
    shown: tuple[str, ...],
    completion: str,
) -> None:
    instance = make_instance(tmp_path)
    outcome = CommandResult(
        **({"ok": True} | result), instance=instance, log_path=instance.log_path
    )
    calls: list[tuple[str, Any]] = []

    def resolve(**requested: Any) -> ServerInstance:
        calls.append(("resolve", requested))
        return instance

    def service(name: str) -> Any:
        def run(resolved: ServerInstance) -> CommandResult:
            calls.append((name, resolved))
            return outcome

        return run

    exit_code = cli_main.run(
        ["server", command, *flags],
        resolve=resolve,
        start=service("start"),
        stop=service("stop"),
        status=service("status"),
    )

    assert exit_code == code
    assert calls == [("resolve", target), (command, instance)]
    lines = capsys.readouterr().out.splitlines()
    for line in (f"command: server {command}", f"url: {URL}", f"data_dir: {tmp_path / 'data'}"):
        assert line in lines
    for line in shown:
        assert line in lines
    # Stop reports neither log nor WebUI; start omits an unknown WebUI state, status never.
    assert (f"log_path: {instance.log_path}" in lines) is (command != "stop")
    webui_shown = any(line.startswith("webui:") for line in lines)
    assert webui_shown is (command == "status" or "webui" in result)
    assert lines[-1] == completion


def test_server_command_announces_the_action_before_dispatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = make_instance(tmp_path)

    def start(resolved: ServerInstance) -> CommandResult:
        assert URL in capsys.readouterr().out
        return CommandResult(ok=True, message="started", instance=resolved)

    exit_code = cli_main.run(["server", "start"], resolve=lambda **_target: instance, start=start)

    assert exit_code == 0
    assert URL in capsys.readouterr().out


@pytest.mark.parametrize("stopped", ["stopped", "not running"])
def test_restart_stops_then_resolves_again_and_starts(tmp_path: Path, stopped: str) -> None:
    calls: list[str] = []
    instances = iter([make_instance(tmp_path, port=8001), make_instance(tmp_path, port=8002)])

    def resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        calls.append(f"resolve:{host}:{port}:{data_dir}")
        return next(instances)

    def stop(instance: ServerInstance) -> CommandResult:
        calls.append(f"stop:{instance.port}")
        return CommandResult(ok=True, message=stopped, instance=instance)

    def start(instance: ServerInstance) -> CommandResult:
        calls.append(f"start:{instance.port}")
        return CommandResult(ok=True, message="started", instance=instance)

    exit_code = cli_main.run(
        ["server", "restart", "--port", "8765", "--data-dir", "data"],
        resolve=resolve,
        start=start,
        stop=stop,
    )

    assert exit_code == 0
    assert calls == [
        "resolve:127.0.0.1:8765:data",
        "stop:8001",
        "resolve:127.0.0.1:8765:data",
        "start:8002",
    ]


def test_restart_does_not_start_when_stop_fails(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    def start(unused: ServerInstance) -> CommandResult:
        raise AssertionError("restart must not start after a failed stop")

    def stop(resolved: ServerInstance) -> CommandResult:
        return CommandResult(ok=False, message=CONFLICT, instance=resolved, health=FOREIGN_HEALTH)

    exit_code = cli_main.run(
        ["server", "restart"], resolve=lambda **_target: instance, start=start, stop=stop
    )

    assert exit_code == 1


@pytest.mark.parametrize(("ok", "prefix"), [(True, "success:"), (False, "error:")])
def test_management_output_is_never_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ok: bool, prefix: str
) -> None:
    cli_main.print_management_command_result(
        CommandResult(ok=ok, message="", instance=make_instance(tmp_path))
    )

    assert capsys.readouterr().out.startswith(prefix)


def test_main_exits_with_the_run_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_commands, "run", lambda argv: 7)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["server", "status"])

    assert exc_info.value.code == 7


def test_console_output_replaces_a_legacy_windows_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LegacyStream:
        def __init__(self) -> None:
            self.encoding = "cp1252"
            self.errors = "strict"

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self.encoding = encoding
            self.errors = errors

    stdout = LegacyStream()
    stderr = LegacyStream()
    monkeypatch.setattr(cli_main.sys, "stdout", stdout)
    monkeypatch.setattr(cli_main.sys, "stderr", stderr)

    cli_main._configure_console_output()

    assert (stdout.encoding, stdout.errors) == ("utf-8", "backslashreplace")
    assert (stderr.encoding, stderr.errors) == ("utf-8", "backslashreplace")
