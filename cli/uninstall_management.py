"""Guided application removal and data reset for ``vbot uninstall``.

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
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cli.autostart_management import DEFAULT_TASK_NAME
from cli.install_state import InstallStateError, read_install_state
from cli.server_management import (
    DEFAULT_SERVICE_NAME,
    CommandResult,
    HealthProbeResult,
    ServerInstance,
    decode_command_output,
    is_systemd_managed,
    probe_health,
    resolve_instance,
    start_server,
    start_systemd_server,
    stop_server,
    stop_systemd_server,
)
from core.utils.config import APP_DIR, DEFAULT_HOST

_COMMAND_TIMEOUT_SECONDS = 30.0
_DATA_REMOVE_ATTEMPTS = 3
_DATA_REMOVE_RETRY_SECONDS = 1.0


class UninstallMode(Enum):
    """User-visible removal scopes supported by ``vbot uninstall``."""

    APP_ONLY = "app-only"
    DATA_ONLY = "data-only"
    ALL = "all"


@dataclass(frozen=True)
class UninstallResult:
    """Outcome of a selected uninstall or data-reset operation."""

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
ResolveInstance = Callable[..., ServerInstance]
ProbeHealth = Callable[[ServerInstance], HealthProbeResult]
ServerLifecycle = Callable[[ServerInstance], CommandResult]
SystemdManaged = Callable[[str], bool]
SystemdLifecycle = Callable[[ServerInstance, str], CommandResult]
Input = Callable[[str], str]
Output = Callable[[str], None]
RemoveDirectory = Callable[[Path], None]
Launcher = Callable[..., UninstallResult]


def launch_uninstall(
    *,
    task_name: str = DEFAULT_TASK_NAME,
    service_name: str = DEFAULT_SERVICE_NAME,
    remove_data: bool = False,
    data_directory: Path | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
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

    install_root = (root or APP_DIR).resolve()
    current_directory = (working_directory or Path.cwd()).resolve()
    home = (home_directory or Path.home()).resolve()
    if remove_data and (data_directory is None or server_host is None or server_port is None):
        return _fail(
            "uninstall: deleting data requires an exact data directory, server host, and port"
        )
    if current_directory.is_relative_to(install_root):
        return _fail(
            "uninstall: the current directory is inside the installation; "
            "change to another directory and run 'vbot uninstall' again"
        )

    if platform == "win32":
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
            remove_data=remove_data,
            data_directory=data_directory,
            server_host=server_host,
            server_port=server_port,
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
        arguments = [
            bash,
            str(script),
            "--remove-autostart",
            "--service-name",
            service_name,
        ]
        if data_directory is not None and server_host is not None and server_port is not None:
            arguments.extend(
                [
                    "--data-dir",
                    str(data_directory),
                    "--host",
                    str(server_host),
                    "--port",
                    str(server_port),
                ]
            )
        if remove_data:
            arguments.append("--remove-data")
        try:
            change_directory(home)
            execv(bash, arguments)
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
    remove_data: bool,
    data_directory: Path | None,
    server_host: str | None,
    server_port: int | None,
    runner: Runner,
    working_directory: Path,
) -> UninstallResult:
    helper_script = _windows_helper_script(
        script,
        task_name=task_name,
        process_id=process_id,
        remove_data=remove_data,
        data_directory=data_directory,
        server_host=server_host,
        server_port=server_port,
    )
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
            f"uninstall: application removal started for {script.parent.parent} in an "
            "elevated PowerShell window; this confirms only that the helper launched, not "
            "that removal completed; completion or failure will be reported in that window; "
            + (
                "the selected vBot data directory will also be deleted"
                if remove_data
                else "the vBot data directory will be preserved"
            )
        ),
    )


def _windows_helper_script(
    script: Path,
    *,
    task_name: str,
    process_id: int,
    remove_data: bool = False,
    data_directory: Path | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
) -> str:
    uninstall_arguments = f"-RemoveAutostart -TaskName {_powershell_literal(task_name)}"
    if data_directory is not None and server_host is not None and server_port is not None:
        uninstall_arguments += (
            f" -DataDirectory {_powershell_literal(str(data_directory))}"
            f" -ServerHost {_powershell_literal(server_host)} -ServerPort {server_port}"
        )
    if remove_data:
        if data_directory is None or server_host is None or server_port is None:
            raise ValueError("data removal requires a complete server target")
        uninstall_arguments += " -RemoveData"
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"Wait-Process -Id {process_id} -ErrorAction SilentlyContinue\n"
        "try {\n"
        f"    & {_powershell_literal(str(script))} {uninstall_arguments}\n"
        "}\n"
        "catch {\n"
        "    Write-Error ('vBot uninstall failed: ' + $_.Exception.Message)\n"
        "    Write-Host 'Press Enter to close this window.'\n"
        "    [void](Read-Host)\n"
        "    exit 1\n"
        "}\n"
    )


def run_uninstall(
    *,
    mode: UninstallMode | None = None,
    assume_yes: bool = False,
    host: str | None = None,
    port: int | None = None,
    data_dir: str | Path | None = None,
    task_name: str = DEFAULT_TASK_NAME,
    service_name: str = DEFAULT_SERVICE_NAME,
    platform: str = sys.platform,
    root: Path | None = None,
    interactive: bool | None = None,
    input_fn: Input = input,
    output_fn: Output = print,
    resolve: ResolveInstance = resolve_instance,
    probe: ProbeHealth = probe_health,
    stop: ServerLifecycle = stop_server,
    start: ServerLifecycle = start_server,
    systemd_managed: SystemdManaged = is_systemd_managed,
    stop_systemd: SystemdLifecycle = stop_systemd_server,
    start_systemd: SystemdLifecycle = start_systemd_server,
    remove_directory: RemoveDirectory | None = None,
    launcher: Launcher = launch_uninstall,
    working_directory: Path | None = None,
    home_directory: Path | None = None,
) -> UninstallResult:
    """Select, confirm, and perform one safe application/data removal scope."""

    install_root = (root or APP_DIR).resolve()
    current_directory = (working_directory or Path.cwd()).resolve()
    home = (home_directory or Path.home()).resolve()
    effective_interactive = sys.stdin.isatty() if interactive is None else interactive

    try:
        instance = _resolve_uninstall_instance(
            install_root,
            host=host,
            port=port,
            data_dir=data_dir,
            resolve=resolve,
        )
    except (InstallStateError, OSError, ValueError) as exc:
        return _fail(f"uninstall: could not resolve the installation target: {exc}")

    selected = mode
    if selected is None:
        if not effective_interactive:
            return _fail(
                "uninstall: choose --app-only, --data-only, or --all when stdin is not interactive"
            )
        selection = _choose_mode(instance.data_dir, input_fn=input_fn, output_fn=output_fn)
        if isinstance(selection, UninstallResult):
            return selection
        selected = selection

    data_safety_error = _data_safety_error(
        instance.data_dir,
        install_root=install_root,
        current_directory=current_directory,
        home_directory=home,
    )
    if selected in {UninstallMode.DATA_ONLY, UninstallMode.ALL} and data_safety_error:
        return _fail(f"uninstall: refusing to delete the data directory: {data_safety_error}")
    if (
        selected is UninstallMode.APP_ONLY
        and _removes_install_root(install_root)
        and instance.data_dir.is_relative_to(install_root)
    ):
        return _fail(
            "uninstall: the data directory is inside the managed installation and cannot be "
            "preserved; move it first or choose --all"
        )

    if not assume_yes:
        if not effective_interactive:
            return _fail("uninstall: confirmation requires an interactive terminal or --yes")
        confirmation = _confirm_mode(
            selected,
            instance.data_dir,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        if isinstance(confirmation, UninstallResult):
            return confirmation

    if selected is UninstallMode.DATA_ONLY:
        return reset_data_directory(
            instance,
            service_name=service_name,
            probe=probe,
            stop=stop,
            start=start,
            systemd_managed=systemd_managed,
            stop_systemd=stop_systemd,
            start_systemd=start_systemd,
            remove_directory=remove_directory or _remove_data_directory,
        )

    stop_error = _stop_application_server(
        instance,
        install_root=install_root,
        service_name=service_name,
        probe=probe,
        stop=stop,
        systemd_managed=systemd_managed,
        stop_systemd=stop_systemd,
    )
    if stop_error is not None:
        return stop_error

    return launcher(
        task_name=task_name,
        service_name=service_name,
        remove_data=selected is UninstallMode.ALL,
        data_directory=instance.data_dir,
        server_host=instance.host,
        server_port=instance.port,
        platform=platform,
        root=install_root,
        working_directory=current_directory,
        home_directory=home,
    )


def _stop_application_server(
    instance: ServerInstance,
    *,
    install_root: Path,
    service_name: str,
    probe: ProbeHealth,
    stop: ServerLifecycle,
    systemd_managed: SystemdManaged,
    stop_systemd: SystemdLifecycle,
) -> UninstallResult | None:
    """Stop the selected application's server before handing its files to a remover."""

    health = probe(instance)
    unit_owned = systemd_managed(service_name)
    if unit_owned:
        stopped = stop_systemd(instance, service_name)
    elif health.is_vbot:
        stopped = stop(instance)
    else:
        return None
    if stopped.ok:
        return None
    return _fail(
        "uninstall: application removal aborted because the server could not be stopped: "
        f"{stopped.message}; application directory preserved: {install_root}"
    )


def reset_data_directory(
    instance: ServerInstance,
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    probe: ProbeHealth = probe_health,
    stop: ServerLifecycle = stop_server,
    start: ServerLifecycle = start_server,
    systemd_managed: SystemdManaged = is_systemd_managed,
    stop_systemd: SystemdLifecycle = stop_systemd_server,
    start_systemd: SystemdLifecycle = start_systemd_server,
    remove_directory: RemoveDirectory | None = None,
) -> UninstallResult:
    """Delete one data directory while preserving the target's prior running state."""

    health = probe(instance)
    unit_owned = systemd_managed(service_name)
    was_running = health.is_vbot or unit_owned
    if unit_owned:
        stopped = stop_systemd(instance, service_name)
    elif health.is_vbot:
        stopped = stop(instance)
    else:
        stopped = None
    if stopped is not None and not stopped.ok:
        return _fail(
            f"uninstall: data reset aborted because the server could not be stopped: "
            f"{stopped.message}"
        )

    remover = remove_directory or _remove_data_directory
    try:
        remover(instance.data_dir)
    except OSError as exc:
        suffix = "; the server remains stopped" if was_running else ""
        return _fail(f"uninstall: could not delete {instance.data_dir}: {exc}{suffix}")

    if not was_running:
        return UninstallResult(
            ok=True,
            message=(
                f"uninstall: data directory reset complete: {instance.data_dir}; "
                "the server remains stopped"
            ),
        )

    restarted = start_systemd(instance, service_name) if unit_owned else start(instance)
    if not restarted.ok:
        return _fail(
            f"uninstall: data directory reset complete, but the server could not be "
            f"restarted: {restarted.message}"
        )
    return UninstallResult(
        ok=True,
        message=(
            f"uninstall: data directory reset complete: {instance.data_dir}; "
            "the server was restarted with fresh data"
        ),
    )


def _resolve_uninstall_instance(
    root: Path,
    *,
    host: str | None,
    port: int | None,
    data_dir: str | Path | None,
    resolve: ResolveInstance,
) -> ServerInstance:
    state = read_install_state(root)
    return resolve(
        host=host or (state.server_host if state else None) or DEFAULT_HOST,
        port=port if port is not None else (state.server_port if state else None),
        data_dir=(
            data_dir if data_dir is not None else (state.server_data_directory if state else None)
        ),
    )


def _choose_mode(
    data_directory: Path,
    *,
    input_fn: Input,
    output_fn: Output,
) -> UninstallMode | UninstallResult:
    output_fn("What do you want to remove?")
    output_fn(f"  1) Application only (keep data at {data_directory})")
    output_fn(f"  2) Data only (reset {data_directory}, keep the application)")
    output_fn("  3) Application and data")
    output_fn("  4) Cancel")
    choices = {
        "1": UninstallMode.APP_ONLY,
        "2": UninstallMode.DATA_ONLY,
        "3": UninstallMode.ALL,
    }
    while True:
        try:
            answer = input_fn("Selection [4]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return _cancelled()
        if answer in {"", "4", "q", "quit", "cancel"}:
            return _cancelled()
        selected = choices.get(answer)
        if selected is not None:
            return selected
        output_fn("Enter 1, 2, 3, or 4.")


def _confirm_mode(
    mode: UninstallMode,
    data_directory: Path,
    *,
    input_fn: Input,
    output_fn: Output,
) -> None | UninstallResult:
    if mode is UninstallMode.APP_ONLY:
        prompt = "Remove the vBot application and keep its data? Type YES to continue: "
        expected = "YES"
    else:
        output_fn("WARNING: This permanently deletes settings, credentials, Agents, and Sessions.")
        output_fn(f"Data directory: {data_directory}")
        prompt = "Type DELETE to continue: "
        expected = "DELETE"
    try:
        answer = input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return _cancelled()
    if answer != expected:
        return _cancelled()
    return None


def _data_safety_error(
    data_directory: Path,
    *,
    install_root: Path,
    current_directory: Path,
    home_directory: Path,
) -> str | None:
    target = data_directory.resolve()
    filesystem_root = Path(target.anchor).resolve()
    if target == filesystem_root:
        return f"{target} is a filesystem root"
    if target == home_directory:
        return f"{target} is the user home directory"
    if install_root == target or install_root.is_relative_to(target):
        return f"{target} contains the vBot installation"
    if current_directory == target or current_directory.is_relative_to(target):
        return f"the current directory is inside {target}; change directory first"
    return None


def _remove_data_directory(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")
    for attempt in range(1, _DATA_REMOVE_ATTEMPTS + 1):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == _DATA_REMOVE_ATTEMPTS:
                raise
            time.sleep(_DATA_REMOVE_RETRY_SECONDS)


def _removes_install_root(root: Path) -> bool:
    return (root / ".vbot-install-root").is_file() or (root / ".vbot-bootstrap").is_file()


def _cancelled() -> UninstallResult:
    return UninstallResult(ok=True, message="uninstall: cancelled; no changes made")


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


def _fail(message: str) -> UninstallResult:
    return UninstallResult(ok=False, message=message)
