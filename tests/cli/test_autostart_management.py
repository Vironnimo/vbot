"""Tests for the local `vbot autostart` command logic."""

from __future__ import annotations

import sys
import sysconfig
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from cli import autostart_management
from cli.autostart_management import (
    CommandRun,
    _default_runner,
    autostart_status,
    disable_autostart,
    enable_autostart,
    windows_autostart_main,
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


def test_default_runner_decodes_utf8_and_preserves_invalid_bytes() -> None:
    utf8_result = _default_runner(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write('Łódź'.encode('utf-8'))",
        ]
    )
    invalid_result = _default_runner(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.buffer.write(bytes([0x81]) + b'tail')",
        ]
    )

    assert utf8_result.stdout == "Łódź"
    assert invalid_result.stderr == r"\x81tail"


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
        windows_launcher_path=r"C:\Program Files\vbot\vbot-autostart.exe",
    )

    assert result.ok, result.message
    assert "running" in result.message
    assert events == ["start"]
    create = runner.first("schtasks", "/Create")
    assert create is not None
    action = create[create.index("/TR") + 1]
    assert action == (
        f'"C:\\Program Files\\vbot\\vbot-autostart.exe" '
        f"--host 127.0.0.1 --port 8420 --data-dir {inst.data_dir}"
    )
    assert "ONLOGON" in create


def test_enable_windows_failure_hints_elevation() -> None:
    runner = ScriptedRunner(lambda command: _err("Access is denied"))
    events, start = _recording_start()

    result = enable_autostart(
        _instance(),
        platform="win32",
        runner=runner,
        start=start,
        windows_launcher_path=r"C:\vbot-autostart.exe",
    )

    assert not result.ok
    assert "elevated" in result.message.lower()
    assert "server: running" in result.message
    assert events == ["start"]


def test_windows_launcher_resolution_prefers_active_environment_over_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    environment_launcher = scripts_dir / "vbot-autostart.exe"
    environment_launcher.touch()
    monkeypatch.setattr(sysconfig, "get_path", lambda name: str(scripts_dir))
    monkeypatch.setattr(
        autostart_management.shutil,
        "which",
        lambda name: r"C:\unrelated-python\Scripts\vbot-autostart.exe",
    )

    resolved = autostart_management._resolve_windows_autostart_path()

    assert resolved == str(environment_launcher)


def test_windows_autostart_launcher_runs_server_start_without_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[str] = []

    def run_cli(argv: Sequence[str]) -> int:
        captured.extend(argv)
        print("hidden status output")
        return 7

    with pytest.raises(SystemExit) as raised:
        windows_autostart_main(
            ["--host", "127.0.0.1", "--port", "8420", "--data-dir", r"C:\vBot Data"],
            run_cli=run_cli,
        )

    assert raised.value.code == 7
    assert captured == [
        "server",
        "start",
        "--host",
        "127.0.0.1",
        "--port",
        "8420",
        "--data-dir",
        r"C:\vBot Data",
    ]
    assert capsys.readouterr().out == ""


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
    assert "start requested via the service" in result.message
    assert events == []  # Linux starts via systemctl --now, not the managed start
    unit = (tmp_path / "vbot.service").read_text(encoding="utf-8")
    assert 'ExecStart="/usr/bin/python3" "-m" "server.main"' in unit
    assert '"--host" "127.0.0.1" "--port" "8420"' in unit
    escaped_repo = str(repo).replace("\\", "\\\\")
    assert f'WorkingDirectory="{escaped_repo}"' in unit
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
        return _err() if "/TN" in command else _ok()

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
    runner = ScriptedRunner(lambda command: CommandRun(1, "disabled", ""))
    result = autostart_status(_instance(), platform="linux", runner=runner)

    assert result.ok
    assert "not enabled" in result.message


def test_linux_service_name_cannot_escape_unit_directory(tmp_path: Path) -> None:
    runner = ScriptedRunner(lambda command: _ok())

    result = enable_autostart(
        _instance(),
        platform="linux",
        runner=runner,
        unit_dir=tmp_path,
        service_name="../../outside",
    )

    assert not result.ok
    assert "invalid systemd service name" in result.message
    assert list(tmp_path.iterdir()) == []
    assert runner.calls == []


def test_linux_unit_quotes_paths_and_escapes_specifiers(tmp_path: Path) -> None:
    runner = ScriptedRunner(lambda command: _ok())
    instance = _instance()
    instance = ServerInstance(
        host=instance.host,
        port=instance.port,
        data_dir=Path("/home/user/My Data/%n"),
        url=instance.url,
        log_path=instance.log_path,
    )

    result = enable_autostart(
        instance,
        platform="linux",
        runner=runner,
        unit_dir=tmp_path,
        python_executable="/home/user/My Python/bin/python",
        repo_root=Path("/home/user/My Repo/%i"),
    )

    assert result.ok, result.message
    unit = (tmp_path / "vbot.service").read_text(encoding="utf-8")
    escaped_repo = str(Path("/home/user/My Repo/%i")).replace("%", "%%").replace("\\", "\\\\")
    escaped_data = str(instance.data_dir).replace("%", "%%").replace("\\", "\\\\")
    assert f'WorkingDirectory="{escaped_repo}"' in unit
    assert 'ExecStart="/home/user/My Python/bin/python"' in unit
    assert f'"--data-dir" "{escaped_data}"' in unit


def test_linux_disable_failure_preserves_unit(tmp_path: Path) -> None:
    unit = tmp_path / "vbot.service"
    unit.write_text("[Unit]\n", encoding="utf-8")

    def handler(command: list[str]) -> CommandRun:
        if "disable" in command:
            return _err("dbus unavailable")
        return _ok()

    result = disable_autostart(
        _instance(), platform="linux", runner=ScriptedRunner(handler), unit_dir=tmp_path
    )

    assert not result.ok
    assert "systemctl disable failed" in result.message
    assert unit.exists()


def test_linux_status_surfaces_systemctl_failure() -> None:
    result = autostart_status(
        _instance(), platform="linux", runner=ScriptedRunner(lambda command: _err("no bus"))
    )

    assert not result.ok
    assert "systemctl is-enabled failed" in result.message


def test_linger_failure_is_reported_without_rolling_back_enabled_unit(tmp_path: Path) -> None:
    def handler(command: list[str]) -> CommandRun:
        if command[0] == "loginctl":
            return _err("permission denied")
        return _ok()

    result = enable_autostart(
        _instance(),
        platform="linux",
        runner=ScriptedRunner(handler),
        unit_dir=tmp_path,
    )

    assert result.ok
    assert "boot-before-login is not guaranteed" in result.message
    assert (tmp_path / "vbot.service").exists()


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
