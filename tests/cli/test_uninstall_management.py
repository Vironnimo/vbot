"""Tests for the local ``vbot uninstall`` process handoff."""

from __future__ import annotations

import base64
from pathlib import Path

from cli.install_state import INSTALL_STATE_SCHEMA_VERSION, InstallState, write_install_state
from cli.server_management import CommandResult, HealthProbeResult, ServerInstance
from cli.uninstall_management import (
    CommandRun,
    UninstallMode,
    UninstallResult,
    _encode_powershell,
    _windows_helper_script,
    launch_uninstall,
    run_uninstall,
)
from core.utils.logging import resolve_daily_log_path


def _install_root(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "uninstall.ps1").write_text("# test\n", encoding="utf-8")
    (scripts / "uninstall.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return root


def _decode_powershell(encoded: str) -> str:
    return base64.b64decode(encoded).decode("utf-16-le")


def _instance(tmp_path: Path) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=data_dir,
        url="http://127.0.0.1:8420",
        log_path=resolve_daily_log_path(data_dir),
    )


def _command_result(
    instance: ServerInstance, *, ok: bool = True, message: str = "ok"
) -> CommandResult:
    return CommandResult(ok=ok, message=message, instance=instance)


def _record_command(calls: list[str], label: str, instance: ServerInstance) -> CommandResult:
    calls.append(label)
    return _command_result(instance)


def test_windows_launches_elevated_helper_outside_install(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    calls: list[tuple[list[str], Path]] = []

    def runner(command: list[str], working_directory: Path) -> CommandRun:
        calls.append((command, working_directory))
        return CommandRun(0, "", "")

    result = launch_uninstall(
        platform="win32",
        root=root,
        runner=runner,
        powershell_path=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        process_id=4321,
        working_directory=tmp_path,
        home_directory=tmp_path,
        task_name="Custom vBot",
    )

    assert result.ok
    assert "elevated PowerShell window" in result.message
    assert calls[0][1] == tmp_path
    command = calls[0][0]
    assert command[0].endswith("powershell.exe")
    elevation_script = _decode_powershell(command[-1])
    assert "Start-Process" in elevation_script
    assert "-Verb RunAs" in elevation_script
    assert "-WindowStyle Normal" in elevation_script
    expected_helper = _windows_helper_script(
        root / "scripts" / "uninstall.ps1", task_name="Custom vBot", process_id=4321
    )
    assert _encode_powershell(expected_helper) in elevation_script


def test_windows_helper_waits_for_cli_and_quotes_task_name(tmp_path: Path) -> None:
    script = tmp_path / "vBot's install" / "scripts" / "uninstall.ps1"

    helper = _windows_helper_script(script, task_name="Team's vBot", process_id=987)

    assert "Wait-Process -Id 987" in helper
    assert "vBot''s install" in helper
    assert "-RemoveAutostart" in helper
    assert "-TaskName 'Team''s vBot'" in helper


def test_windows_helper_forwards_exact_data_and_server_target(tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "uninstall.ps1"
    data_dir = tmp_path / "user data"

    helper = _windows_helper_script(
        script,
        task_name="vBot",
        process_id=987,
        remove_data=True,
        data_directory=data_dir,
        server_host="127.0.0.1",
        server_port=9000,
    )

    assert "-RemoveData" in helper
    assert f"-DataDirectory '{data_dir}'" in helper
    assert "-ServerHost '127.0.0.1'" in helper
    assert "-ServerPort 9000" in helper


def test_windows_refuses_to_uninstall_from_inside_install(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    calls: list[list[str]] = []

    def runner(command: list[str], _working_directory: Path) -> CommandRun:
        calls.append(command)
        return CommandRun(0, "", "")

    result = launch_uninstall(
        platform="win32",
        root=root,
        runner=runner,
        powershell_path="powershell.exe",
        working_directory=root / "nested",
        home_directory=tmp_path,
    )

    assert not result.ok
    assert "current directory is inside" in result.message
    assert calls == []


def test_windows_surfaces_elevation_failure(tmp_path: Path) -> None:
    root = _install_root(tmp_path)

    result = launch_uninstall(
        platform="win32",
        root=root,
        runner=lambda _command, _cwd: CommandRun(1, "", "UAC cancelled"),
        powershell_path="powershell.exe",
        working_directory=tmp_path,
        home_directory=tmp_path,
    )

    assert not result.ok
    assert "UAC cancelled" in result.message


def test_linux_replaces_cli_with_bundled_uninstaller(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    exec_calls: list[tuple[str, list[str]]] = []
    changed_to: list[Path] = []

    def execv(executable: str, arguments: list[str]) -> None:
        exec_calls.append((executable, arguments))

    result = launch_uninstall(
        platform="linux",
        root=root,
        execv=execv,
        change_directory=lambda path: changed_to.append(Path(path)),
        bash_path="/bin/bash",
        working_directory=tmp_path,
        home_directory=tmp_path,
        service_name="custom-vbot",
    )

    assert not result.ok
    assert "returned without replacing" in result.message
    assert changed_to == [tmp_path]
    assert exec_calls == [
        (
            "/bin/bash",
            [
                "/bin/bash",
                str(root / "scripts" / "uninstall.sh"),
                "--remove-autostart",
                "--service-name",
                "custom-vbot",
            ],
        )
    ]


def test_linux_all_mode_forwards_exact_data_and_server_target(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    exec_calls: list[list[str]] = []

    result = launch_uninstall(
        platform="linux",
        root=root,
        execv=lambda _executable, arguments: exec_calls.append(arguments),
        change_directory=lambda _path: None,
        bash_path="/bin/bash",
        working_directory=tmp_path,
        home_directory=tmp_path,
        remove_data=True,
        data_directory=tmp_path / "data",
        server_host="127.0.0.1",
        server_port=9000,
        service_name="custom-vbot",
    )

    assert not result.ok
    assert exec_calls[0][-7:] == [
        "--data-dir",
        str(tmp_path / "data"),
        "--host",
        "127.0.0.1",
        "--port",
        "9000",
        "--remove-data",
    ]


def test_uninstall_reports_missing_script_and_unsupported_platform(tmp_path: Path) -> None:
    missing = launch_uninstall(
        platform="win32",
        root=tmp_path,
        powershell_path="powershell.exe",
        working_directory=tmp_path.parent,
    )
    unsupported = launch_uninstall(platform="darwin", root=tmp_path)

    assert not missing.ok
    assert "uninstaller not found" in missing.message
    assert not unsupported.ok
    assert "not supported" in unsupported.message


def test_interactive_data_reset_restarts_previously_running_server(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    instance = _instance(tmp_path)
    answers = iter(["2", "DELETE"])
    output: list[str] = []
    calls: list[str] = []

    result = run_uninstall(
        root=root,
        interactive=True,
        input_fn=lambda _prompt: next(answers),
        output_fn=output.append,
        resolve=lambda **_kwargs: instance,
        probe=lambda _instance: HealthProbeResult(reachable=True, is_vbot=True),
        stop=lambda _instance: _record_command(calls, "stop", instance),
        start=lambda _instance: _record_command(calls, "start", instance),
        systemd_managed=lambda _name: False,
        remove_directory=lambda path: calls.append(f"remove:{path}"),
    )

    assert result.ok
    assert calls == ["stop", f"remove:{instance.data_dir}", "start"]
    assert output[0] == "What do you want to remove?"
    assert any("WARNING" in line for line in output)
    assert "restarted with fresh data" in result.message


def test_data_reset_keeps_previously_stopped_server_stopped(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    instance = _instance(tmp_path)
    calls: list[str] = []

    result = run_uninstall(
        mode=UninstallMode.DATA_ONLY,
        assume_yes=True,
        root=root,
        resolve=lambda **_kwargs: instance,
        probe=lambda _instance: HealthProbeResult(reachable=False, is_vbot=False),
        stop=lambda _instance: _record_command(calls, "unexpected-stop", instance),
        start=lambda _instance: _record_command(calls, "unexpected-start", instance),
        systemd_managed=lambda _name: False,
        remove_directory=lambda path: calls.append(f"remove:{path}"),
    )

    assert result.ok
    assert calls == [f"remove:{instance.data_dir}"]
    assert "remains stopped" in result.message


def test_data_reset_preserves_systemd_ownership(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    instance = _instance(tmp_path)
    calls: list[str] = []

    result = run_uninstall(
        mode=UninstallMode.DATA_ONLY,
        assume_yes=True,
        root=root,
        service_name="custom-vbot",
        resolve=lambda **_kwargs: instance,
        probe=lambda _instance: HealthProbeResult(reachable=True, is_vbot=True),
        stop=lambda _instance: _record_command(calls, "unexpected-stop", instance),
        start=lambda _instance: _record_command(calls, "unexpected-start", instance),
        systemd_managed=lambda name: name == "custom-vbot",
        stop_systemd=lambda _instance, name: _record_command(
            calls, f"systemd-stop:{name}", instance
        ),
        start_systemd=lambda _instance, name: _record_command(
            calls, f"systemd-start:{name}", instance
        ),
        remove_directory=lambda path: calls.append(f"remove:{path}"),
    )

    assert result.ok
    assert calls == [
        "systemd-stop:custom-vbot",
        f"remove:{instance.data_dir}",
        "systemd-start:custom-vbot",
    ]


def test_app_only_preserves_data_and_all_forwards_data_to_launcher(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    instance = _instance(tmp_path)
    launches: list[dict[str, object]] = []

    def launcher(**kwargs: object) -> UninstallResult:
        launches.append(kwargs)
        return UninstallResult(ok=True, message="launched")

    app_result = run_uninstall(
        mode=UninstallMode.APP_ONLY,
        assume_yes=True,
        root=root,
        resolve=lambda **_kwargs: instance,
        launcher=launcher,
    )
    all_result = run_uninstall(
        mode=UninstallMode.ALL,
        assume_yes=True,
        root=root,
        resolve=lambda **_kwargs: instance,
        launcher=launcher,
        working_directory=tmp_path,
        home_directory=tmp_path,
    )

    assert app_result.ok and all_result.ok
    assert launches[0]["remove_data"] is False
    assert launches[0]["data_directory"] == instance.data_dir
    assert launches[1]["remove_data"] is True
    assert launches[1]["data_directory"] == instance.data_dir
    assert launches[1]["server_host"] == instance.host
    assert launches[1]["server_port"] == instance.port


def test_uninstall_requires_explicit_mode_and_confirmation_without_tty(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    instance = _instance(tmp_path)

    missing_mode = run_uninstall(
        root=root,
        interactive=False,
        resolve=lambda **_kwargs: instance,
    )
    missing_confirmation = run_uninstall(
        mode=UninstallMode.APP_ONLY,
        root=root,
        interactive=False,
        resolve=lambda **_kwargs: instance,
    )

    assert not missing_mode.ok
    assert "choose --app-only" in missing_mode.message
    assert not missing_confirmation.ok
    assert "--yes" in missing_confirmation.message


def test_uninstall_defaults_to_recorded_installation_target(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    recorded_data = tmp_path / "recorded-data"
    write_install_state(
        root,
        InstallState(
            schema_version=INSTALL_STATE_SCHEMA_VERSION,
            install_shape="server",
            dependency_groups=("server", "cli"),
            python_executable=str(tmp_path / "python"),
            source_track="release",
            applied_revision="abc123",
            dependency_digest="digest",
            webui_revision=None,
            server_host="0.0.0.0",
            server_port=9000,
            server_data_directory=str(recorded_data),
        ),
    )
    resolved: list[dict[str, object]] = []

    def resolve(**kwargs: object) -> ServerInstance:
        resolved.append(kwargs)
        return _instance(tmp_path)

    result = run_uninstall(
        mode=UninstallMode.APP_ONLY,
        assume_yes=True,
        root=root,
        resolve=resolve,
        launcher=lambda **_kwargs: UninstallResult(ok=True, message="launched"),
    )

    assert result.ok
    assert resolved == [{"host": "0.0.0.0", "port": 9000, "data_dir": str(recorded_data)}]


def test_interactive_cancel_makes_no_changes(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    instance = _instance(tmp_path)
    launched = False

    def launcher(**_kwargs: object) -> UninstallResult:
        nonlocal launched
        launched = True
        return UninstallResult(ok=True, message="launched")

    result = run_uninstall(
        root=root,
        interactive=True,
        input_fn=lambda _prompt: "4",
        output_fn=lambda _line: None,
        resolve=lambda **_kwargs: instance,
        launcher=launcher,
    )

    assert result.ok
    assert "cancelled" in result.message
    assert not launched


def test_data_reset_refuses_protected_or_live_paths(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    data_dir = tmp_path / "data"
    protected = ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=data_dir,
        url="http://127.0.0.1:8420",
        log_path=data_dir / "log",
    )

    result = run_uninstall(
        mode=UninstallMode.DATA_ONLY,
        assume_yes=True,
        root=root,
        resolve=lambda **_kwargs: protected,
        working_directory=data_dir / "nested",
        home_directory=tmp_path.parent,
    )

    assert not result.ok
    assert "current directory is inside" in result.message


def test_data_reset_aborts_before_delete_when_stop_fails(tmp_path: Path) -> None:
    root = _install_root(tmp_path)
    instance = _instance(tmp_path)
    removed = False

    def remove_directory(_path: Path) -> None:
        nonlocal removed
        removed = True

    result = run_uninstall(
        mode=UninstallMode.DATA_ONLY,
        assume_yes=True,
        root=root,
        resolve=lambda **_kwargs: instance,
        probe=lambda _instance: HealthProbeResult(reachable=True, is_vbot=True),
        stop=lambda _instance: _command_result(instance, ok=False, message="locked"),
        systemd_managed=lambda _name: False,
        remove_directory=remove_directory,
    )

    assert not result.ok
    assert "could not be stopped" in result.message
    assert not removed
