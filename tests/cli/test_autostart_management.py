"""Tests for the local `vbot autostart` command logic."""

from __future__ import annotations

import base64
import json
import re
import subprocess
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
from core.utils.server_control import SHUTDOWN_FAILED_EXIT_CODE, STARTUP_FAILED_EXIT_CODE


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


def _windows_task_script_and_payload(command: list[str]) -> tuple[str, dict[str, str | int]]:
    assert command[0] == "powershell.exe"
    assert command[-2] == "-EncodedCommand"
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    matched = re.search(r'\$payloadToken = "([A-Za-z0-9+/=]+)"', script)
    assert matched is not None
    payload = json.loads(base64.b64decode(matched.group(1)).decode("utf-8"))
    return script, payload


def test_default_runner_runs_without_a_console_and_decodes_its_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "Łódź\n".encode(), b"\x81tail")

    monkeypatch.setattr(autostart_management.subprocess, "run", run)
    monkeypatch.setattr(autostart_management, "subprocess_creation_flags", lambda: 0x08000000)

    result = _default_runner(["powershell.exe"])

    assert (result.stdout, result.stderr) == ("Łódź", r"\x81tail")
    assert captured["creationflags"] == 0x08000000
    assert captured["capture_output"] is True


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
    assert events == ["start"]
    assert len(runner.calls) == 1
    script, payload = _windows_task_script_and_payload(runner.calls[0])
    assert payload == {
        "operation": "enable",
        "task_name": "vBot",
        "launcher": r"C:\Program Files\vbot\vbot-autostart.exe",
        "arguments": f"--host 127.0.0.1 --port 8420 --data-dir {inst.data_dir}",
        "restart_count": 3,
        "restart_interval": "PT1M",
    }
    assert "$definition.Principal.LogonType = 3" in script
    assert "$definition.Principal.RunLevel = 0" in script
    assert "$trigger.UserId = $userId" in script
    assert "$definition.Settings.RestartCount = [int]$payload.restart_count" in script
    assert "$definition.Settings.RestartInterval = [string]$payload.restart_interval" in script
    # Normal CPU, I/O and memory priority instead of the below-normal default 7.
    assert "$definition.Settings.Priority = 4" in script
    assert "$root.RegisterTaskDefinition(" in script


def test_enable_windows_surfaces_per_user_registration_failure() -> None:
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
    assert events == ["start"]


def test_windows_task_values_are_encoded_as_data() -> None:
    task_name = 'vBot"; Write-Output "injected'
    command = autostart_management._windows_task_command("status", task_name=task_name)

    script, payload = _windows_task_script_and_payload(command)

    assert payload == {"operation": "status", "task_name": task_name}
    assert task_name not in script


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


def test_windows_autostart_server_gets_cold_start_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[ServerInstance, float]] = []
    instance = _instance()

    def start_server(target: ServerInstance, *, startup_timeout_seconds: float) -> CommandResult:
        captured.append((target, startup_timeout_seconds))
        return CommandResult(ok=True, message="started", instance=target)

    monkeypatch.setattr(autostart_management, "start_server", start_server)

    result = autostart_management._start_windows_autostart_server(instance)

    assert result.ok
    assert captured == [(instance, 60.0)]


@pytest.mark.parametrize("lingering", [True, False], ids=["lingering", "linger-refused"])
def test_enable_linux_writes_unit_and_enables(tmp_path: Path, lingering: bool) -> None:
    def handler(command: list[str]) -> CommandRun:
        # Lingering is best effort: its failure warns but keeps the enabled unit.
        refused = command[0] == "loginctl" and not lingering
        return _err("permission denied") if refused else _ok()

    runner = ScriptedRunner(handler)
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
    assert events == []  # Linux starts via systemctl --now, not the managed start
    unit = (tmp_path / "vbot.service").read_text(encoding="utf-8")
    assert 'ExecStart="/usr/bin/python3" "-m" "server.main"' in unit
    assert '"--host" "127.0.0.1" "--port" "8420"' in unit
    escaped_repo = str(repo).replace("\\", "\\\\")
    assert f'WorkingDirectory="{escaped_repo}"' in unit
    assert "KillMode=mixed" in unit
    assert "KillMode=process" not in unit
    # A failed startup would fail again, and a stop whose shutdown failed was requested.
    no_restart = f"RestartPreventExitStatus={STARTUP_FAILED_EXIT_CODE} {SHUTDOWN_FAILED_EXIT_CODE}"
    assert f"\n{no_restart}\n" in unit
    assert runner.ran("systemctl", "--user", "enable", "--now", "vbot.service")
    assert runner.ran("loginctl", "enable-linger")
    assert ("login lingering could not be enabled" in result.message) is not lingering
    linger_attention = "Login lingering could not be enabled; boot-before-login is not guaranteed"
    assert (linger_attention in result.attention) is not lingering


def test_enable_unsupported_platform() -> None:
    runner = ScriptedRunner(lambda command: _ok())
    events, start = _recording_start()

    result = enable_autostart(_instance(), platform="darwin", runner=runner, start=start)

    assert not result.ok
    assert events == []


@pytest.mark.parametrize(
    ("status", "operations"),
    [
        pytest.param(_ok(), ["status", "delete"], id="existing-task"),
        pytest.param(CommandRun(3, "", ""), ["status"], id="absent-task"),
    ],
)
def test_disable_windows_deletes_only_an_existing_task(
    status: CommandRun, operations: list[str]
) -> None:
    def handler(command: list[str]) -> CommandRun:
        operation = _windows_task_script_and_payload(command)[1]["operation"]
        return status if operation == "status" else _ok()

    runner = ScriptedRunner(handler)

    result = disable_autostart(_instance(), platform="win32", runner=runner)

    assert result.ok
    called = [_windows_task_script_and_payload(call)[1]["operation"] for call in runner.calls]
    assert called == operations


@pytest.mark.parametrize("disabled", [True, False], ids=["disabled", "disable-fails"])
def test_disable_linux_removes_the_unit_only_after_systemd_disabled_it(
    tmp_path: Path, disabled: bool
) -> None:
    unit = tmp_path / "vbot.service"
    unit.write_text("[Unit]\n", encoding="utf-8")

    def handler(command: list[str]) -> CommandRun:
        return _err("dbus unavailable") if "disable" in command and not disabled else _ok()

    runner = ScriptedRunner(handler)

    result = disable_autostart(_instance(), platform="linux", runner=runner, unit_dir=tmp_path)

    assert result.ok is disabled
    assert unit.exists() is not disabled
    assert runner.ran("systemctl", "--user", "disable", "vbot.service")


_WINDOWS_LAUNCHER = r"C:\vbot\vbot-desktop.exe"


def _task_action(arguments: list[str]) -> CommandRun:
    action = {"launcher": _WINDOWS_LAUNCHER, "arguments": subprocess.list2cmdline(arguments)}
    return _ok(json.dumps(action))


@pytest.mark.parametrize(
    ("answer", "ok", "message"),
    [
        pytest.param(
            _task_action(
                ["--host", "127.0.0.1", "--port", "8420", "--data-dir", str(_instance().data_dir)]
            ),
            True,
            "autostart: enabled (per-user Task Scheduler task 'vBot')",
            id="enabled",
        ),
        pytest.param(
            _task_action(["--host", "127.0.0.1", "--port", "9999", "--data-dir", "/data"]),
            False,
            "targets a different server instance",
            id="other-instance",
        ),
        pytest.param(
            CommandRun(3, "", ""),
            True,
            "autostart: not enabled (per-user Task Scheduler task 'vBot')",
            id="absent",
        ),
        pytest.param(
            _err("no service"),
            False,
            "autostart: Task Scheduler query failed: no service",
            id="scheduler-failure",
        ),
    ],
)
def test_windows_status_matches_the_task_to_the_exact_instance(
    answer: CommandRun, ok: bool, message: str
) -> None:
    result = autostart_status(
        _instance(),
        platform="win32",
        runner=ScriptedRunner(lambda command: answer),
        windows_launcher_path=_WINDOWS_LAUNCHER,
    )

    assert result.ok is ok
    assert message in result.message


_UNIT_PYTHON = "/expected/python"
_UNIT_REPO = Path("/repo")


def _unit_for(instance: ServerInstance, *, older_version: bool = False) -> str:
    """The unit this version writes for *instance*, or the one the previous version wrote."""

    unit = autostart_management._systemd_unit(instance, _UNIT_PYTHON, _UNIT_REPO)
    return re.sub(r"RestartPreventExitStatus=.*\n", "", unit) if older_version else unit


def _other_instance() -> ServerInstance:
    instance = _instance()
    return ServerInstance(
        host=instance.host,
        port=9999,
        data_dir=instance.data_dir,
        url="http://127.0.0.1:9999",
        log_path=instance.log_path,
    )


@pytest.mark.parametrize(
    ("answer", "unit", "ok", "message"),
    [
        pytest.param(CommandRun(1, "disabled", ""), None, True, "not enabled", id="disabled"),
        pytest.param(
            _ok("enabled"),
            "[Service]\nExecStart=/wrong/program\n",
            False,
            "different server instance",
            id="other-instance",
        ),
        pytest.param(
            _ok("enabled"),
            _unit_for(_other_instance(), older_version=True),
            False,
            "different server instance",
            id="other-instance-older-version",
        ),
        pytest.param(_ok("enabled"), _unit_for(_instance()), True, "enabled", id="exact-instance"),
        pytest.param(
            _ok("enabled"),
            _unit_for(_instance(), older_version=True),
            True,
            "is outdated; rewrite it with: vbot autostart enable --host 127.0.0.1 --port 8420",
            id="exact-instance-older-version",
        ),
        pytest.param(_err("no bus"), None, False, "systemctl is-enabled failed", id="failure"),
    ],
)
def test_linux_status_matches_the_enabled_unit_to_the_exact_instance(
    tmp_path: Path, answer: CommandRun, unit: str | None, ok: bool, message: str
) -> None:
    if unit is not None:
        (tmp_path / "vbot.service").write_text(unit, encoding="utf-8")

    result = autostart_status(
        _instance(),
        platform="linux",
        runner=ScriptedRunner(lambda command: answer),
        unit_dir=tmp_path,
        python_executable=_UNIT_PYTHON,
        repo_root=_UNIT_REPO,
    )

    assert result.ok is ok
    assert message in result.message
    # An outdated unit of this server is still enabled, and the result says so.
    assert bool(result.attention) is ("outdated" in message)


@pytest.mark.parametrize(
    ("unit", "reload_ok", "rewritten"),
    [
        pytest.param(_unit_for(_instance(), older_version=True), True, True, id="older-version"),
        pytest.param(_unit_for(_instance()), True, False, id="current"),
        pytest.param(
            _unit_for(_other_instance(), older_version=True), True, False, id="other-instance"
        ),
        pytest.param(None, True, False, id="missing"),
        pytest.param(_unit_for(_instance(), older_version=True), False, False, id="reload-fails"),
    ],
)
def test_linux_refresh_rewrites_only_this_installations_outdated_unit(
    tmp_path: Path, unit: str | None, reload_ok: bool, rewritten: bool
) -> None:
    unit_path = tmp_path / "vbot.service"
    if unit is not None:
        unit_path.write_text(unit, encoding="utf-8")
    runner = ScriptedRunner(lambda command: _ok() if reload_ok else _err("no bus"))

    result = autostart_management.refresh_autostart_unit(
        _instance(),
        platform="linux",
        runner=runner,
        unit_dir=tmp_path,
        python_executable=_UNIT_PYTHON,
        repo_root=_UNIT_REPO,
    )

    assert result.ok is reload_ok
    # An empty message means nothing changed.
    assert bool(result.message) is (rewritten or not reload_ok)
    expected = _unit_for(_instance()) if rewritten else unit
    assert (unit_path.read_text(encoding="utf-8") if unit_path.exists() else None) == expected
    # Only a rewrite reloads systemd; enablement is left alone either way, and a
    # failed reload restores the previous file.
    assert (runner.calls != []) is (rewritten or not reload_ok)
    assert all(call[:3] == ["systemctl", "--user", "daemon-reload"] for call in runner.calls)


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


def test_dispatch_autostart_passes_the_parsed_options_to_enable() -> None:
    captured: dict[str, object] = {}

    def enable_fn(instance: ServerInstance, **kwargs: object) -> CommandResult:
        captured.update(kwargs)
        return CommandResult(ok=True, message="enabled", instance=instance)

    def start(instance: ServerInstance) -> CommandResult:
        return CommandResult(ok=True, message="started", instance=instance)

    args = parse_args(["autostart", "enable", "--task-name", "MyTask"])
    result = dispatch_autostart_command(
        args, resolve=lambda **_kwargs: _instance(), start=start, enable_fn=enable_fn
    )

    assert result.ok
    assert (captured["task_name"], captured["service_name"]) == ("MyTask", None)
    assert captured["start"] is start
