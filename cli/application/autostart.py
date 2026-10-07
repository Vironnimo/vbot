"""Per-user logon registration of one packaged installation's server.

Windows registers a Task Scheduler logon task that runs the stable bootstrap
(``vBot.exe``) without arguments, which starts the tray. Linux registers the
systemd user unit ``vbot.service`` that runs the stable bootstrap's server mode
(``<root>/vbot --server``), plus login lingering so it starts at boot. Each
registration belongs to exactly one installation root; another root's entry is
never changed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli.application.state import ApplicationError, Installation, ensure_not_removing
from core.utils.atomic import atomic_write_text
from core.utils.logging import CONSOLE_LOGGING_ENV_VAR
from core.utils.processes import subprocess_creation_flags
from core.utils.server_control import SHUTDOWN_FAILED_EXIT_CODE, STARTUP_FAILED_EXIT_CODE

ACTIONS = frozenset({"status", "enable", "disable"})
#: The systemd user unit of a packaged Linux server.
UNIT_NAME = "vbot.service"
#: Exit status of the Linux bootstrap when the installation has no valid active version.
BOOTSTRAP_FAILED_EXIT_CODE = 111
# Cap every registration command so a stuck Task Scheduler call or polkit
# prompt cannot block forever.
_COMMAND_TIMEOUT_SECONDS = 30.0
_TIMED_OUT_EXIT_CODE = 124
_TASK_NOT_FOUND_EXIT_CODE = 3
_LOGGER = logging.getLogger("vbot.application.integration")

_TASK_SCRIPT = r"""
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

    if ($payload.operation -eq "inspect") {
        try {
            $task = $root.GetTask([string]$payload.task_name)
            $actions = @($task.Definition.Actions | Where-Object { $_.Type -eq 0 })
            if ($actions.Count -ne 1) {
                throw "Expected exactly one executable action"
            }
            [Console]::Out.WriteLine((@{
                launcher = [string]$actions[0].Path
                arguments = [string]$actions[0].Arguments
            } | ConvertTo-Json -Compress))
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
    # The default task priority 7 starts the process tree below normal, with
    # low I/O and memory priority: Windows trims the idle server's working set
    # and pages it back in at low I/O priority, stalling the Event Loop for
    # seconds. Priority 4 is the normal class with normal I/O and memory priority.
    $definition.Settings.Priority = 4
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


@dataclass(frozen=True)
class CommandRun:
    """Result of one external registration command."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], CommandRun]


def run_command(command: list[str]) -> CommandRun:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            creationflags=subprocess_creation_flags(),
        )
    except subprocess.TimeoutExpired:
        return CommandRun(
            _TIMED_OUT_EXIT_CODE,
            "",
            f"command timed out after {_COMMAND_TIMEOUT_SECONDS:.0f}s: {command[0]}",
        )
    except OSError as exc:
        return CommandRun(127, "", f"could not run {command[0]}: {exc}")
    return CommandRun(
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace").strip(),
        completed.stderr.decode("utf-8", errors="replace").strip(),
    )


def same_path(left: str | Path, right: str | Path) -> bool:
    try:
        # Windows process APIs may report an existing executable through its
        # 8.3 alias even when the installation record uses the long path.
        return os.path.samefile(left, right)
    except OSError:
        # Registry and Task Scheduler ownership checks also compare targets
        # that may not exist. Retain a stable lexical comparison for those.
        pass
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def autostart(
    install: Installation,
    action: str,
    *,
    platform: str = sys.platform,
    runner: Runner | None = None,
    unit_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect or change this installation's per-user logon registration."""
    if action not in ACTIONS:
        raise ApplicationError("Unknown application Autostart action")
    if action == "enable":
        ensure_not_removing(install.root)
    run = runner or run_command
    if platform == "win32":
        return _task_autostart(install, action, run)
    if platform.startswith("linux"):
        if not install.owns_server:
            raise ApplicationError("A Desktop Client has no server to start at logon")
        return _unit_autostart(install, action, run, unit_dir or user_unit_dir())
    raise ApplicationError(f"Packaged application Autostart is not supported on {platform}")


# Windows Task Scheduler


def task_name(install: Installation) -> str:
    """Return a stable Task Scheduler name scoped to the exact install root."""
    identity = hashlib.sha256(os.path.normcase(str(install.root)).encode("utf-8")).hexdigest()[:12]
    return f"vBot Application {identity}"


def task_command(operation: str, **values: str | int) -> list[str]:
    payload = {"operation": operation, **values}
    payload_token = base64.b64encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    script = _TASK_SCRIPT.replace("__VBOT_PAYLOAD__", payload_token)
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded_script,
    ]


def _task_lookup(run: Runner, name: str) -> tuple[bool, str, str]:
    result = run(task_command("inspect", task_name=name))
    if result.returncode == _TIMED_OUT_EXIT_CODE:
        # A cold powershell.exe start occasionally hangs past the timeout. The
        # query changes nothing, so it is safe to ask once more.
        _LOGGER.warning("Task Scheduler query timed out; asking once more")
        result = run(task_command("inspect", task_name=name))
    if result.returncode == _TASK_NOT_FOUND_EXIT_CODE:
        return False, "", ""
    if result.returncode != 0:
        detail = result.stdout or result.stderr or f"exit code {result.returncode}"
        raise ApplicationError(f"Task Scheduler query failed: {detail}")
    try:
        value = json.loads(result.stdout)
        launcher, arguments = value["launcher"], value["arguments"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ApplicationError("Task Scheduler returned invalid application ownership") from exc
    if not isinstance(launcher, str) or not isinstance(arguments, str):
        raise ApplicationError("Task Scheduler returned invalid application ownership")
    return True, launcher, arguments


def _task_autostart(install: Installation, action: str, run: Runner) -> dict[str, Any]:
    name = task_name(install)
    launcher = install.root / "vBot.exe"
    if not launcher.is_file():
        raise ApplicationError("Application bootstrap is missing")
    exists, registered_launcher, arguments = _task_lookup(run, name)
    owned = exists and same_path(registered_launcher, launcher) and arguments == ""
    if exists and not owned:
        raise ApplicationError(f"Task Scheduler entry '{name}' belongs to another application")
    if action == "status":
        return {"ok": True, "enabled": owned, "task_name": name, "launcher": str(launcher)}
    if action == "disable":
        if not exists:
            return {"ok": True, "enabled": False, "task_name": name, "changed": False}
        result = run(task_command("delete", task_name=name))
        if result.returncode not in {0, _TASK_NOT_FOUND_EXIT_CODE}:
            raise ApplicationError(
                f"Could not disable application Autostart: {result.stdout or result.stderr}"
            )
        _LOGGER.info("Application Autostart disabled (mode=task_scheduler)")
        return {"ok": True, "enabled": False, "task_name": name, "changed": True}
    result = run(
        task_command(
            "enable",
            task_name=name,
            launcher=str(launcher),
            arguments="",
            restart_count=3,
            restart_interval="PT1M",
        )
    )
    if result.returncode != 0:
        raise ApplicationError(
            f"Could not enable application Autostart: {result.stdout or result.stderr}"
        )
    verified, verified_launcher, verified_arguments = _task_lookup(run, name)
    if not verified or not same_path(verified_launcher, launcher) or verified_arguments:
        raise ApplicationError("Application Autostart registration could not be verified")
    if not owned:
        _LOGGER.info("Application Autostart enabled (mode=task_scheduler)")
    return {"ok": True, "enabled": True, "task_name": name, "changed": not owned}


# Linux systemd user unit


def user_unit_dir() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured and Path(configured).is_absolute() else None
    return (base or Path.home() / ".config") / "systemd" / "user"


def _quote(value: str) -> str:
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def unit_content(install: Installation) -> str:
    """The unit that runs this installation's active version as its server."""
    assert install.server_host is not None and install.server_port is not None
    assert install.server_data_directory is not None
    command = " ".join(
        _quote(value)
        for value in (
            str(install.bootstrap),
            "--server",
            "--host",
            install.server_host,
            "--port",
            str(install.server_port),
            "--data-dir",
            install.server_data_directory,
        )
    )
    return (
        "[Unit]\n"
        "Description=vBot server\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        # The server writes its daily log; the journal keeps only output that
        # bypasses logging, such as a crash before logging starts.
        f"Environment={CONSOLE_LOGGING_ENV_VAR}=0\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        # A failed startup would fail again, a stop whose Runtime shutdown failed
        # was requested, and a bootstrap without a valid version cannot recover.
        "RestartPreventExitStatus="
        f"{STARTUP_FAILED_EXIT_CODE} {SHUTDOWN_FAILED_EXIT_CODE} {BOOTSTRAP_FAILED_EXIT_CODE}\n"
        # Stop the server gracefully, then remove every remaining server-owned
        # descendant from the service cgroup.
        "KillMode=mixed\n"
        "TimeoutStopSec=60\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _unit_launcher(content: str) -> str | None:
    """The program a unit's single ``ExecStart=`` runs, if it has exactly one."""
    values = [
        line.removeprefix("ExecStart=").strip()
        for line in content.splitlines()
        if line.startswith("ExecStart=")
    ]
    if len(values) != 1:
        return None
    try:
        command = shlex.split(values[0])
    except ValueError:
        return None
    return command[0].replace("%%", "%") if command else None


def owned_unit(install: Installation, *, unit_dir: Path | None = None) -> Path | None:
    """The unit file when it runs this installation's bootstrap, else ``None``."""
    if not sys.platform.startswith("linux") and unit_dir is None:
        return None
    path = (unit_dir or user_unit_dir()) / UNIT_NAME
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    launcher = _unit_launcher(content)
    return path if launcher is not None and same_path(launcher, install.bootstrap) else None


def _systemctl(run: Runner, *arguments: str) -> CommandRun:
    return run(["systemctl", "--user", *arguments])


def _unit_autostart(install: Installation, action: str, run: Runner, units: Path) -> dict[str, Any]:
    path = units / UNIT_NAME
    exists = path.exists()
    owned = owned_unit(install, unit_dir=units) is not None
    if exists and not owned:
        raise ApplicationError(
            f"The systemd user unit {UNIT_NAME} belongs to another vBot installation"
        )
    if action == "status":
        return {"ok": True, "enabled": owned, "unit": UNIT_NAME, "launcher": str(install.bootstrap)}
    if action == "disable":
        if not exists:
            return {"ok": True, "enabled": False, "unit": UNIT_NAME, "changed": False}
        disabled = _systemctl(run, "disable", UNIT_NAME)
        if disabled.returncode != 0:
            raise ApplicationError(
                f"Could not disable application Autostart: {disabled.stderr or disabled.stdout}"
            )
        path.unlink()
        _systemctl(run, "daemon-reload")
        _LOGGER.info("Application Autostart disabled (mode=systemd)")
        return {"ok": True, "enabled": False, "unit": UNIT_NAME, "changed": True}
    if not install.bootstrap.is_file():
        raise ApplicationError("Application bootstrap is missing")
    content = unit_content(install)
    previous = path.read_text(encoding="utf-8") if exists else None
    if previous != content:
        units.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content)
    for step in (("daemon-reload",), ("enable", UNIT_NAME)):
        result = _systemctl(run, *step)
        if result.returncode != 0:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, previous)
            _systemctl(run, "daemon-reload")
            raise ApplicationError(
                "Could not enable application Autostart: "
                f"systemctl --user {' '.join(step)} failed: {result.stderr or result.stdout}"
            )
    # Lingering starts the user's units at boot without a login. It may need
    # privileges the user lacks, so the registration stands without it.
    lingered = run(["loginctl", "enable-linger"])
    value: dict[str, Any] = {
        "ok": True,
        "enabled": True,
        "unit": UNIT_NAME,
        "changed": previous != content,
        "linger": lingered.returncode == 0,
    }
    if lingered.returncode != 0:
        value["attention"] = (
            "Login lingering could not be enabled, so the server starts only once you log in: "
            f"{lingered.stderr or lingered.stdout or f'exit code {lingered.returncode}'}"
        )
    if previous != content:
        _LOGGER.info("Application Autostart enabled (mode=systemd)")
    return value
