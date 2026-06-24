"""Tests for the local `vbot autostart` command logic."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cli.autostart_management import (
    CommandRun,
    autostart_status,
    disable_autostart,
    enable_autostart,
)
from cli.main import dispatch_autostart_command
from cli.parser import parse_args
from cli.server_management import CommandResult, ServerInstance


def _instance() -> ServerInstance:
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=Path("/data"),
        url="http://127.0.0.1:8420",
        log_path=Path("/data/logs/today.log"),
    )


def _ok(stdout: str = "") -> CommandRun:
    return CommandRun(returncode=0, stdout=stdout, stderr="")


def _err(stderr: str = "Access is denied") -> CommandRun:
    return CommandRun(returncode=1, stdout="", stderr=stderr)


class ScriptedRunner:
    """Records command invocations and answers from a per-command handler."""

    def __init__(self, handler: Callable[[list[str]], CommandRun]) -> None:
        self._handler = handler
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> CommandRun:
        self.calls.append(list(command))
        return self._handler(list(command))

    def ran(self, *needle: str) -> bool:
        target = list(needle)
        return any(
            call[index : index + len(target)] == target
            for call in self.calls
            for index in range(len(call) - len(target) + 1)
        )

    def first(self, *needle: str) -> list[str] | None:
        target = list(needle)
        for call in self.calls:
            if any(call[i : i + len(target)] == target for i in range(len(call))):
                return call
        return None


def _recording_start() -> tuple[list[str], Callable[[ServerInstance], CommandResult]]:
    events: list[str] = []

    def start(instance: ServerInstance) -> CommandResult:
        events.append("start")
        return CommandResult(ok=True, message="started", instance=instance)

    return events, start


def test_enable_windows_creates_task_and_starts() -> None:
    runner = ScriptedRunner(lambda command: _ok())
    events, start = _recording_start()
    inst = _instance()

    result = enable_autostart(
        inst,
        platform="win32",
        runner=runner,
        start=start,
        vbot_path=r"C:\Program Files\vbot\vbot.exe",
    )

    assert result.ok, result.message
    assert "running" in result.message
    assert events == ["start"]
    create = runner.first("schtasks", "/Create")
    assert create is not None
    action = create[create.index("/TR") + 1]
    assert action == (
        f'"C:\\Program Files\\vbot\\vbot.exe" server start '
        f'--host 127.0.0.1 --port 8420 --data-dir "{inst.data_dir}"'
    )
    assert "ONLOGON" in create


def test_enable_windows_failure_hints_elevation() -> None:
    runner = ScriptedRunner(lambda command: _err("Access is denied"))
    events, start = _recording_start()

    result = enable_autostart(
        _instance(), platform="win32", runner=runner, start=start, vbot_path=r"C:\vbot.exe"
    )

    assert not result.ok
    assert "elevated" in result.message.lower()
    assert events == []


def test_enable_linux_writes_unit_and_enables(tmp_path: Path) -> None:
    runner = ScriptedRunner(lambda command: _ok())
    events, start = _recording_start()

    repo = Path("/opt/vbot")
    result = enable_autostart(
        _instance(),
        platform="linux",
        runner=runner,
        start=start,
        unit_dir=tmp_path,
        python_executable="/usr/bin/python3",
        repo_root=repo,
    )

    assert result.ok, result.message
    assert "started via the service" in result.message
    assert events == []  # Linux starts via systemctl --now, not the managed start
    unit = (tmp_path / "vbot.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3 -m server.main --host 127.0.0.1 --port 8420" in unit
    assert f"WorkingDirectory={repo}" in unit
    assert runner.ran("systemctl", "--user", "enable", "--now", "vbot.service")


def test_enable_unsupported_platform() -> None:
    runner = ScriptedRunner(lambda command: _ok())
    events, start = _recording_start()

    result = enable_autostart(_instance(), platform="darwin", runner=runner, start=start)

    assert not result.ok
    assert "not supported" in result.message
    assert events == []


def test_disable_windows_deletes_existing_task() -> None:
    def handler(command: list[str]) -> CommandRun:
        return _ok() if command[:2] == ["schtasks", "/Query"] else _ok()

    runner = ScriptedRunner(handler)
    result = disable_autostart(_instance(), platform="win32", runner=runner)

    assert result.ok
    assert "removed" in result.message
    assert runner.ran("schtasks", "/Delete")


def test_disable_windows_idempotent_when_absent() -> None:
    def handler(command: list[str]) -> CommandRun:
        return _err() if command[:2] == ["schtasks", "/Query"] else _ok()

    runner = ScriptedRunner(handler)
    result = disable_autostart(_instance(), platform="win32", runner=runner)

    assert result.ok
    assert "already disabled" in result.message
    assert not runner.ran("schtasks", "/Delete")


def test_disable_linux_removes_unit(tmp_path: Path) -> None:
    (tmp_path / "vbot.service").write_text("[Unit]\n", encoding="utf-8")
    runner = ScriptedRunner(lambda command: _ok())

    result = disable_autostart(_instance(), platform="linux", runner=runner, unit_dir=tmp_path)

    assert result.ok
    assert "removed" in result.message
    assert not (tmp_path / "vbot.service").exists()
    assert runner.ran("systemctl", "--user", "disable", "vbot.service")


def test_status_reports_enabled_windows() -> None:
    runner = ScriptedRunner(lambda command: _ok())
    result = autostart_status(_instance(), platform="win32", runner=runner)

    assert result.ok
    assert "enabled" in result.message
    assert "not enabled" not in result.message


def test_status_reports_not_enabled_linux() -> None:
    runner = ScriptedRunner(lambda command: _err("disabled"))
    result = autostart_status(_instance(), platform="linux", runner=runner)

    assert result.ok
    assert "not enabled" in result.message


def test_parse_args_autostart() -> None:
    args = parse_args(["autostart", "enable", "--task-name", "MyTask"])

    assert args.area == "autostart"
    assert args.command == "enable"
    assert args.task_name == "MyTask"
    assert args.service_name is None


def test_dispatch_autostart_routes_to_enable() -> None:
    captured: dict[str, object] = {}

    def enable_fn(instance: ServerInstance, **kwargs: object) -> CommandResult:
        captured.update(kwargs)
        return CommandResult(ok=True, message="enabled", instance=instance)

    def start(instance: ServerInstance) -> CommandResult:
        return CommandResult(ok=True, message="started", instance=instance)

    args = parse_args(["autostart", "enable"])
    result = dispatch_autostart_command(
        args, resolve=lambda **_kwargs: _instance(), start=start, enable_fn=enable_fn
    )

    assert result.ok
    assert captured["task_name"] is None
    assert "start" in captured
