"""Safe handoff from ``vbot uninstall`` to the platform uninstall script.

The installed CLI lives inside the environment that a managed uninstall removes.
On Windows it therefore cannot wait for the removal in-process: the executable is
locked until this process exits. The command launches an elevated PowerShell
helper which waits for this CLI process, then runs the existing uninstaller. On
Linux, ``execv`` replaces the CLI process with the Bash uninstaller directly.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cli.autostart_management import DEFAULT_TASK_NAME
from cli.server_management import DEFAULT_SERVICE_NAME, decode_command_output

_COMMAND_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class UninstallResult:
    """Outcome of launching the platform uninstaller."""

    ok: bool
    message: str


@dataclass(frozen=True)
class CommandRun:
    """Captured result of the short Windows elevation-launch command."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], Path], CommandRun]
Exec = Callable[[str, list[str]], object]
ChangeDirectory = Callable[[str | os.PathLike[str]], None]


def launch_uninstall(
    *,
    task_name: str = DEFAULT_TASK_NAME,
    service_name: str = DEFAULT_SERVICE_NAME,
    platform: str = sys.platform,
    root: Path | None = None,
    runner: Runner | None = None,
    execv: Exec = os.execv,
    change_directory: ChangeDirectory = os.chdir,
    powershell_path: str | None = None,
    bash_path: str | None = None,
    process_id: int | None = None,
    working_directory: Path | None = None,
    home_directory: Path | None = None,
) -> UninstallResult:
    """Launch the checkout's platform uninstaller without retaining file locks."""

    install_root = (root or _repo_root()).resolve()
    current_directory = (working_directory or Path.cwd()).resolve()
    home = (home_directory or Path.home()).resolve()

    if platform == "win32":
        if current_directory.is_relative_to(install_root):
            return _fail(
                "uninstall: the current directory is inside the installation; "
                "change to another directory and run 'vbot uninstall' again"
            )
        script = install_root / "scripts" / "uninstall.ps1"
        if not script.is_file():
            return _fail(f"uninstall: Windows uninstaller not found: {script}")
        powershell = powershell_path or _resolve_powershell()
        if powershell is None:
            return _fail("uninstall: could not locate PowerShell")
        return _launch_windows_uninstaller(
            powershell,
            script,
            task_name=task_name,
            process_id=process_id or os.getpid(),
            runner=runner or _default_runner,
            working_directory=home,
        )

    if platform.startswith("linux"):
        script = install_root / "scripts" / "uninstall.sh"
        if not script.is_file():
            return _fail(f"uninstall: Linux uninstaller not found: {script}")
        bash = bash_path or shutil.which("bash")
        if bash is None:
            return _fail("uninstall: could not locate bash")
        try:
            change_directory(home)
            execv(
                bash,
                [
                    bash,
                    str(script),
                    "--remove-autostart",
                    "--service-name",
                    service_name,
                ],
            )
        except OSError as exc:
            return _fail(f"uninstall: could not start {script}: {exc}")
        return _fail("uninstall: Linux uninstaller returned without replacing the CLI process")

    return _fail(f"uninstall: not supported on this platform ({platform})")


def _launch_windows_uninstaller(
    powershell: str,
    script: Path,
    *,
    task_name: str,
    process_id: int,
    runner: Runner,
    working_directory: Path,
) -> UninstallResult:
    helper_script = _windows_helper_script(script, task_name=task_name, process_id=process_id)
    helper_arguments = [
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        _encode_powershell(helper_script),
    ]
    helper_command_line = subprocess.list2cmdline(helper_arguments)
    elevation_script = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        f"    Start-Process -FilePath {_powershell_literal(powershell)} -Verb RunAs "
        f"-ArgumentList {_powershell_literal(helper_command_line)} "
        "-WindowStyle Normal | Out-Null\n"
        "}\n"
        "catch {\n"
        "    Write-Error ('uninstall: elevation was not granted: ' + $_.Exception.Message)\n"
        "    exit 1\n"
        "}\n"
    )
    command = [
        powershell,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        _encode_powershell(elevation_script),
    ]
    result = runner(command, working_directory)
    if result.returncode != 0:
        detail = result.stderr or result.stdout or f"exit code {result.returncode}"
        return _fail(f"uninstall: could not launch the elevated uninstaller: {detail}")
    return UninstallResult(
        ok=True,
        message=(
            "uninstall: launched in an elevated PowerShell window; removal continues after "
            "this command exits; the vBot data directory will be preserved"
        ),
    )


def _windows_helper_script(script: Path, *, task_name: str, process_id: int) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"Wait-Process -Id {process_id} -ErrorAction SilentlyContinue\n"
        "try {\n"
        f"    & {_powershell_literal(str(script))} -RemoveAutostart "
        f"-TaskName {_powershell_literal(task_name)}\n"
        "}\n"
        "catch {\n"
        "    Write-Error ('vBot uninstall failed: ' + $_.Exception.Message)\n"
        "    Write-Host 'Press Enter to close this window.'\n"
        "    [void](Read-Host)\n"
        "    exit 1\n"
        "}\n"
    )


def _encode_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _resolve_powershell() -> str | None:
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        windows_powershell = (
            Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        )
        if windows_powershell.is_file():
            return str(windows_powershell)
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe")


def _default_runner(command: list[str], working_directory: Path) -> CommandRun:
    try:
        completed = subprocess.run(
            command,
            cwd=working_directory,
            capture_output=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return CommandRun(
            returncode=124,
            stdout="",
            stderr=f"elevation request timed out after {_COMMAND_TIMEOUT_SECONDS:.0f}s",
        )
    except OSError as exc:
        return CommandRun(returncode=127, stdout="", stderr=str(exc))
    return CommandRun(
        returncode=completed.returncode,
        stdout=decode_command_output(completed.stdout).strip(),
        stderr=decode_command_output(completed.stderr).strip(),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fail(message: str) -> UninstallResult:
    return UninstallResult(ok=False, message=message)
