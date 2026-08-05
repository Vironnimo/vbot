"""Local OS autostart management for the vBot server.

``vbot autostart enable|disable|status`` registers, removes, or inspects an
OS-level autostart entry for the server. It is a local lifecycle command like
``server`` and ``update`` — it acts on this machine, not through RPC. Windows
uses a Task Scheduler logon task; Linux uses a systemd **user** unit plus login
lingering. ``enable`` also brings the server up immediately (Linux via the unit,
Windows via a managed background start), so the machine ends up both running and
boot-persistent in one step.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from cli.server_management import (
    DEFAULT_SERVICE_NAME,
    CommandResult,
    ServerInstance,
    decode_command_output,
    is_valid_systemd_service_name,
    start_server,
)
from core.utils.config import VBOT_ROOT

DEFAULT_TASK_NAME = "vBot"

_WINDOWS_POWERSHELL = "powershell.exe"
_WINDOWS_TASK_NOT_FOUND_EXIT_CODE = 3
_WINDOWS_AUTOSTART_STARTUP_TIMEOUT_SECONDS = 60.0
_WINDOWS_TASK_RESTART_COUNT = 3
_WINDOWS_TASK_RESTART_INTERVAL = "PT1M"
_WINDOWS_TASK_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$payloadToken = "__VBOT_PAYLOAD__"
$payloadJson = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($payloadToken)
)
$payload = $payloadJson | ConvertFrom-Json

function Test-TaskNotFound {
    param([System.Management.Automation.ErrorRecord]$Record)

    $exception = $Record.Exception
    while ($null -ne $exception) {
        $errorCode = [int64]$exception.HResult -band [int64]0xFFFFFFFF
        if ($errorCode -eq [int64]0x80070002) {
            return $true
        }
        $exception = $exception.InnerException
    }
    return $false
}

try {
    $service = New-Object -ComObject "Schedule.Service"
    $service.Connect()
    $root = $service.GetFolder("\")

    if ($payload.operation -eq "status") {
        try {
            $null = $root.GetTask([string]$payload.task_name)
            exit 0
        }
        catch {
            if (Test-TaskNotFound -Record $_) {
                exit 3
            }
            throw
        }
    }

    if ($payload.operation -eq "delete") {
        try {
            $root.DeleteTask([string]$payload.task_name, 0)
            exit 0
        }
        catch {
            if (Test-TaskNotFound -Record $_) {
                exit 3
            }
            throw
        }
    }

    if ($payload.operation -ne "enable") {
        throw "Unsupported Task Scheduler operation: $($payload.operation)"
    }

    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $definition = $service.NewTask(0)
    $definition.Principal.UserId = $userId
    $definition.Principal.LogonType = 3
    $definition.Principal.RunLevel = 0
    $definition.Settings.AllowDemandStart = $true
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $definition.Settings.StartWhenAvailable = $true
    $definition.Settings.ExecutionTimeLimit = "PT0S"
    $definition.Settings.MultipleInstances = 2
    $definition.Settings.RestartCount = [int]$payload.restart_count
    $definition.Settings.RestartInterval = [string]$payload.restart_interval

    $trigger = $definition.Triggers.Create(9)
    $trigger.Enabled = $true
    $trigger.UserId = $userId

    $action = $definition.Actions.Create(0)
    $action.Path = [string]$payload.launcher
    $action.Arguments = [string]$payload.arguments

    $null = $root.RegisterTaskDefinition(
        [string]$payload.task_name,
        $definition,
        6,
        $userId,
        $null,
        3,
        $null
    )
    exit 0
}
catch {
    # Windows PowerShell can serialize progress records onto stderr when its
    # streams are redirected. Keep the actionable error on clean stdout.
    [Console]::Out.WriteLine($_.Exception.Message)
    exit 1
}
"""

# Cap every autostart command so a stuck Task Scheduler call,
# `systemctl enable --now`, or polkit prompt cannot block forever.
_COMMAND_TIMEOUT_SECONDS = 30.0

Restart = Callable[[ServerInstance], CommandResult]


@dataclass(frozen=True)
class CommandRun:
    """Result of one external command invocation."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], CommandRun]


@dataclass(frozen=True)
class _Step:
    ok: bool
    message: str


@dataclass(frozen=True)
class _TaskLookup:
    ok: bool
    exists: bool
    message: str = ""


def enable_autostart(
    instance: ServerInstance,
    *,
    platform: str = sys.platform,
    runner: Runner | None = None,
    start: Restart = start_server,
    task_name: str | None = None,
    service_name: str | None = None,
    unit_dir: Path | None = None,
    windows_launcher_path: str | None = None,
    python_executable: str = sys.executable,
    repo_root: Path | None = None,
) -> CommandResult:
    """Register OS autostart for the server and start it now."""

    run = runner or _default_runner
    if platform == "win32":
        registered = _windows_enable(
            instance,
            run,
            task_name=task_name or DEFAULT_TASK_NAME,
            launcher_path=windows_launcher_path,
        )
        started_by_service = False
    elif platform.startswith("linux"):
        name = service_name or DEFAULT_SERVICE_NAME
        if not is_valid_systemd_service_name(name):
            return _invalid_service_name(instance)
        registered = _linux_enable(
            instance,
            run,
            service_name=name,
            unit_dir=unit_dir,
            python_executable=python_executable,
            repo_root=repo_root or VBOT_ROOT,
        )
        started_by_service = True
    else:
        return _fail(instance, f"autostart: not supported on this platform ({platform})")

    if not registered.ok:
        if platform != "win32":
            return _fail(instance, registered.message)
        # Registration and the immediate server start are independent so an
        # otherwise successful install remains usable after a Scheduler error.
        start_result = start(instance)
        state = "running" if start_result.ok else f"start failed ({start_result.message})"
        return CommandResult(
            ok=False,
            message=f"{registered.message}; server: {state}",
            instance=instance,
            health=start_result.health,
            webui=start_result.webui,
            log_path=start_result.log_path,
            process_id=start_result.process_id,
        )

    if started_by_service:
        return CommandResult(
            ok=True,
            message=(
                f"autostart enabled ({registered.message}); server start requested via the service"
            ),
            instance=instance,
        )

    start_result = start(instance)
    state = "running" if start_result.ok else f"start failed ({start_result.message})"
    return CommandResult(
        ok=start_result.ok,
        message=f"autostart enabled ({registered.message}); server: {state}",
        instance=instance,
    )


def disable_autostart(
    instance: ServerInstance,
    *,
    platform: str = sys.platform,
    runner: Runner | None = None,
    task_name: str | None = None,
    service_name: str | None = None,
    unit_dir: Path | None = None,
) -> CommandResult:
    """Remove the OS autostart entry, leaving any running server untouched."""

    run = runner or _default_runner
    if platform == "win32":
        return _windows_disable(instance, run, task_name=task_name or DEFAULT_TASK_NAME)
    if platform.startswith("linux"):
        name = service_name or DEFAULT_SERVICE_NAME
        if not is_valid_systemd_service_name(name):
            return _invalid_service_name(instance)
        return _linux_disable(instance, run, service_name=name, unit_dir=unit_dir)
    return _fail(instance, f"autostart: not supported on this platform ({platform})")


def autostart_status(
    instance: ServerInstance,
    *,
    platform: str = sys.platform,
    runner: Runner | None = None,
    task_name: str | None = None,
    service_name: str | None = None,
) -> CommandResult:
    """Report whether OS autostart is registered for the server."""

    run = runner or _default_runner
    if platform == "win32":
        name = task_name or DEFAULT_TASK_NAME
        lookup = _windows_task_lookup(run, name)
        if not lookup.ok:
            return _fail(instance, f"autostart: Task Scheduler query failed: {lookup.message}")
        state = "enabled" if lookup.exists else "not enabled"
        return CommandResult(
            ok=True,
            message=f"autostart: {state} (per-user Task Scheduler task '{name}')",
            instance=instance,
        )
    if platform.startswith("linux"):
        name = service_name or DEFAULT_SERVICE_NAME
        if not is_valid_systemd_service_name(name):
            return _invalid_service_name(instance)
        query = run(["systemctl", "--user", "is-enabled", f"{name}.service"])
        reported_state = query.stdout.strip().lower()
        enabled = query.returncode == 0 and reported_state in {"enabled", "enabled-runtime"}
        known_disabled_states = {
            "disabled",
            "generated",
            "indirect",
            "masked",
            "not-found",
            "static",
            "transient",
        }
        if not enabled and reported_state not in known_disabled_states:
            detail = query.stderr or query.stdout or f"exit code {query.returncode}"
            return _fail(instance, f"autostart: systemctl is-enabled failed: {detail}")
        state = "enabled" if enabled else "not enabled"
        return CommandResult(
            ok=True,
            message=f"autostart: {state} (systemd user unit '{name}')",
            instance=instance,
        )
    return _fail(instance, f"autostart: not supported on this platform ({platform})")


def _windows_enable(
    instance: ServerInstance, run: Runner, *, task_name: str, launcher_path: str | None
) -> _Step:
    launcher = launcher_path or _resolve_windows_autostart_path()
    if launcher is None:
        return _Step(False, "autostart: could not locate the windowless vBot launcher")
    arguments = subprocess.list2cmdline(
        [
            "--host",
            instance.host,
            "--port",
            str(instance.port),
            "--data-dir",
            str(instance.data_dir),
        ]
    )
    created = run(
        _windows_task_command(
            "enable",
            task_name=task_name,
            launcher=launcher,
            arguments=arguments,
            restart_count=_WINDOWS_TASK_RESTART_COUNT,
            restart_interval=_WINDOWS_TASK_RESTART_INTERVAL,
        )
    )
    if created.returncode != 0:
        detail = created.stdout or created.stderr or f"exit code {created.returncode}"
        return _Step(
            False,
            f"autostart: creating the per-user Task Scheduler task failed ({detail})",
        )
    return _Step(True, f"per-user Task Scheduler task '{task_name}' at logon")


def _windows_disable(instance: ServerInstance, run: Runner, *, task_name: str) -> CommandResult:
    lookup = _windows_task_lookup(run, task_name)
    if not lookup.ok:
        return _fail(instance, f"autostart: Task Scheduler query failed: {lookup.message}")
    if not lookup.exists:
        return CommandResult(
            ok=True,
            message=f"autostart already disabled (no Task Scheduler task '{task_name}')",
            instance=instance,
        )
    deleted = run(_windows_task_command("delete", task_name=task_name))
    if deleted.returncode == _WINDOWS_TASK_NOT_FOUND_EXIT_CODE:
        return CommandResult(
            ok=True,
            message=f"autostart already disabled (no Task Scheduler task '{task_name}')",
            instance=instance,
        )
    if deleted.returncode != 0:
        detail = deleted.stdout or deleted.stderr or f"exit code {deleted.returncode}"
        return _fail(
            instance, f"autostart: removing the per-user Task Scheduler task failed: {detail}"
        )
    return CommandResult(
        ok=True,
        message=f"autostart disabled (per-user Task Scheduler task '{task_name}' removed)",
        instance=instance,
    )


def _linux_enable(
    instance: ServerInstance,
    run: Runner,
    *,
    service_name: str,
    unit_dir: Path | None,
    python_executable: str,
    repo_root: Path,
) -> _Step:
    units = unit_dir or _systemd_user_dir()
    units.mkdir(parents=True, exist_ok=True)
    unit_path = units / f"{service_name}.service"
    previous_content = unit_path.read_text(encoding="utf-8") if unit_path.exists() else None
    try:
        _write_unit_file(unit_path, _systemd_unit(instance, python_executable, repo_root))
    except OSError as exc:
        return _Step(False, f"autostart: writing {unit_path} failed: {exc}")

    reloaded = run(["systemctl", "--user", "daemon-reload"])
    if reloaded.returncode != 0:
        rollback = _restore_unit_file(unit_path, previous_content)
        run(["systemctl", "--user", "daemon-reload"])
        rollback_note = f"; rollback failed: {rollback}" if rollback else ""
        return _Step(
            False,
            f"autostart: systemctl daemon-reload failed: "
            f"{reloaded.stderr or reloaded.stdout}{rollback_note}",
        )
    enabled = run(["systemctl", "--user", "enable", "--now", f"{service_name}.service"])
    if enabled.returncode != 0:
        rollback = _restore_unit_file(unit_path, previous_content)
        run(["systemctl", "--user", "daemon-reload"])
        detail = enabled.stderr or enabled.stdout
        rollback_note = f"; rollback failed: {rollback}" if rollback else ""
        return _Step(False, f"autostart: systemctl enable failed: {detail}{rollback_note}")
    # Login lingering lets the user service run at boot without an active login;
    # best-effort, since it can require privileges the user may not have.
    lingered = run(["loginctl", "enable-linger"])
    if lingered.returncode != 0:
        detail = lingered.stderr or lingered.stdout or f"exit code {lingered.returncode}"
        return _Step(
            True,
            f"systemd user unit '{service_name}'; warning: login lingering could not be "
            f"enabled ({detail}), so boot-before-login is not guaranteed",
        )
    return _Step(True, f"systemd user unit '{service_name}' with login lingering")


def _linux_disable(
    instance: ServerInstance, run: Runner, *, service_name: str, unit_dir: Path | None
) -> CommandResult:
    units = unit_dir or _systemd_user_dir()
    unit_path = units / f"{service_name}.service"
    if not unit_path.exists():
        return CommandResult(
            ok=True,
            message=f"autostart already disabled (no systemd user unit '{service_name}')",
            instance=instance,
        )
    disabled = run(["systemctl", "--user", "disable", f"{service_name}.service"])
    if disabled.returncode != 0:
        return _fail(
            instance,
            f"autostart: systemctl disable failed: {disabled.stderr or disabled.stdout}",
        )
    try:
        unit_path.unlink()
    except OSError as exc:
        return _fail(instance, f"autostart: removing {unit_path} failed: {exc}")
    reloaded = run(["systemctl", "--user", "daemon-reload"])
    if reloaded.returncode != 0:
        return _fail(
            instance,
            "autostart unit was removed, but systemctl daemon-reload failed: "
            f"{reloaded.stderr or reloaded.stdout}",
        )
    return CommandResult(
        ok=True,
        message=f"autostart disabled (systemd user unit '{service_name}' removed)",
        instance=instance,
    )


def _systemd_unit(instance: ServerInstance, python_executable: str, repo_root: Path) -> str:
    # Stop the main process gracefully, then let systemd remove every remaining
    # server-owned descendant from the service cgroup after TimeoutStopSec.
    command = " ".join(
        _systemd_quote(value)
        for value in (
            python_executable,
            "-m",
            "server.main",
            "--host",
            instance.host,
            "--port",
            str(instance.port),
            "--data-dir",
            str(instance.data_dir),
        )
    )
    return (
        "[Unit]\n"
        "Description=vBot server\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_systemd_quote(str(repo_root))}\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "KillMode=mixed\n"
        "TimeoutStopSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _resolve_windows_autostart_path() -> str | None:
    import sysconfig

    # The active interpreter identifies the environment that owns this CLI.
    # Prefer its Scripts directory over PATH, which may contain another vBot
    # checkout and silently register the wrong executable for autostart.
    scripts_dir = Path(sysconfig.get_path("scripts"))
    for name in ("vbot-autostart.exe", "vbot-autostart.cmd", "vbot-autostart"):
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)
    found = shutil.which("vbot-autostart")
    if found:
        return found
    return None


def windows_autostart_main(
    argv: Sequence[str] | None = None,
    *,
    run_cli: Callable[[Sequence[str]], int] | None = None,
) -> None:
    """Start the server through the Windows GUI entrypoint without a console."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    with (
        open(os.devnull, "w", encoding="utf-8") as output,
        redirect_stdout(output),
        redirect_stderr(output),
    ):
        if run_cli is None:
            run_cli = _run_windows_autostart_cli
        exit_code = run_cli(["server", "start", *arguments])
    raise SystemExit(exit_code)


def _run_windows_autostart_cli(arguments: Sequence[str]) -> int:
    # Import lazily to avoid the normal CLI's import of this module forming a
    # cycle during startup. Only the boot path gets the longer cold-start budget.
    from cli.main import run

    return run(arguments, start=_start_windows_autostart_server)


def _start_windows_autostart_server(instance: ServerInstance) -> CommandResult:
    return start_server(
        instance,
        startup_timeout_seconds=_WINDOWS_AUTOSTART_STARTUP_TIMEOUT_SECONDS,
    )


def _default_runner(command: list[str]) -> CommandRun:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return CommandRun(
            returncode=124,
            stdout="",
            stderr=f"command timed out after {_COMMAND_TIMEOUT_SECONDS:.0f}s: {' '.join(command)}",
        )
    except OSError as exc:
        return CommandRun(returncode=127, stdout="", stderr=f"could not run {command[0]}: {exc}")
    return CommandRun(
        returncode=completed.returncode,
        stdout=decode_command_output(completed.stdout).strip(),
        stderr=decode_command_output(completed.stderr).strip(),
    )


def _fail(instance: ServerInstance, message: str) -> CommandResult:
    return CommandResult(ok=False, message=message, instance=instance)


def _windows_task_lookup(run: Runner, task_name: str) -> _TaskLookup:
    result = run(_windows_task_command("status", task_name=task_name))
    if result.returncode == 0:
        return _TaskLookup(True, True)
    if result.returncode == _WINDOWS_TASK_NOT_FOUND_EXIT_CODE:
        return _TaskLookup(True, False)
    detail = result.stdout or result.stderr or f"exit code {result.returncode}"
    return _TaskLookup(False, False, detail)


def _windows_task_command(operation: str, **values: str | int) -> list[str]:
    payload = {"operation": operation, **values}
    payload_token = base64.b64encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    script = _WINDOWS_TASK_SCRIPT.replace("__VBOT_PAYLOAD__", payload_token)
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        _WINDOWS_POWERSHELL,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded_script,
    ]


def _write_unit_file(path: Path, content: str) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _restore_unit_file(path: Path, previous_content: str | None) -> str:
    try:
        if previous_content is None:
            path.unlink(missing_ok=True)
        else:
            _write_unit_file(path, previous_content)
    except OSError as exc:
        return str(exc)
    return ""


def _systemd_quote(value: str) -> str:
    escaped = (
        value.replace("%", "%%")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _invalid_service_name(instance: ServerInstance) -> CommandResult:
    return _fail(
        instance,
        "autostart: invalid systemd service name; start with a letter or number, then use "
        "only letters, numbers, '.', '_', '@', or '-', without a .service suffix",
    )
