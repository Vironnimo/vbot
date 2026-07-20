"""Tests for the local ``vbot uninstall`` process handoff."""

from __future__ import annotations

import base64
from pathlib import Path

from cli.uninstall_management import (
    CommandRun,
    _encode_powershell,
    _windows_helper_script,
    launch_uninstall,
)


def _install_root(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "uninstall.ps1").write_text("# test\n", encoding="utf-8")
    (scripts / "uninstall.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return root


def _decode_powershell(encoded: str) -> str:
    return base64.b64decode(encoded).decode("utf-16-le")


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
