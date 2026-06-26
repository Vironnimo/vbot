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

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cli.server_management import (
    DEFAULT_SERVICE_NAME,
    CommandResult,
    ServerInstance,
    start_server,
)

DEFAULT_TASK_NAME = "vBot"

# Cap every autostart command so a stuck `systemctl enable --now`, a polkit
# prompt on `loginctl enable-linger`, or a hung `schtasks` cannot block
# `vbot autostart enable` forever on a headless host.
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


def enable_autostart(
    instance: ServerInstance,
    *,
    platform: str = sys.platform,
    runner: Runner | None = None,
    start: Restart = start_server,
    task_name: str | None = None,
    service_name: str | None = None,
    unit_dir: Path | None = None,
    vbot_path: str | None = None,
    python_executable: str = sys.executable,
    repo_root: Path | None = None,
) -> CommandResult:
    """Register OS autostart for the server and start it now."""

    run = runner or _default_runner
    if platform == "win32":
        registered = _windows_enable(
            instance, run, task_name=task_name or DEFAULT_TASK_NAME, vbot_path=vbot_path
        )
        started_by_service = False
    elif platform.startswith("linux"):
        registered = _linux_enable(
            instance,
            run,
            service_name=service_name or DEFAULT_SERVICE_NAME,
            unit_dir=unit_dir,
            python_executable=python_executable,
            repo_root=repo_root or _repo_root(),
        )
        started_by_service = True
    else:
        return _fail(instance, f"autostart: not supported on this platform ({platform})")

    if not registered.ok:
        return _fail(instance, registered.message)

    if started_by_service:
        return CommandResult(
            ok=True,
            message=f"autostart enabled ({registered.message}); server started via the service",
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
        return _linux_disable(
            instance, run, service_name=service_name or DEFAULT_SERVICE_NAME, unit_dir=unit_dir
        )
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
        query = run(["schtasks", "/Query", "/TN", name])
        state = "enabled" if query.returncode == 0 else "not enabled"
        return CommandResult(
            ok=True,
            message=f"autostart: {state} (Task Scheduler task '{name}')",
            instance=instance,
        )
    if platform.startswith("linux"):
        name = service_name or DEFAULT_SERVICE_NAME
        query = run(["systemctl", "--user", "is-enabled", f"{name}.service"])
        enabled = query.returncode == 0 and query.stdout.strip() == "enabled"
        state = "enabled" if enabled else "not enabled"
        return CommandResult(
            ok=True,
            message=f"autostart: {state} (systemd user unit '{name}')",
            instance=instance,
        )
    return _fail(instance, f"autostart: not supported on this platform ({platform})")


def _windows_enable(
    instance: ServerInstance, run: Runner, *, task_name: str, vbot_path: str | None
) -> _Step:
    vbot = vbot_path or _resolve_vbot_path()
    if vbot is None:
        return _Step(False, "autostart: could not locate the vbot command to schedule")
    action = (
        f'"{vbot}" server start --host {instance.host} '
        f'--port {instance.port} --data-dir "{instance.data_dir}"'
    )
    created = run(["schtasks", "/Create", "/TN", task_name, "/TR", action, "/SC", "ONLOGON", "/F"])
    if created.returncode != 0:
        detail = created.stderr or created.stdout
        return _Step(
            False,
            f"autostart: creating the Task Scheduler task failed ({detail}). "
            "On Windows this usually needs an elevated (Administrator) terminal.",
        )
    return _Step(True, f"Task Scheduler task '{task_name}' at logon")


def _windows_disable(instance: ServerInstance, run: Runner, *, task_name: str) -> CommandResult:
    query = run(["schtasks", "/Query", "/TN", task_name])
    if query.returncode != 0:
        return CommandResult(
            ok=True,
            message=f"autostart already disabled (no Task Scheduler task '{task_name}')",
            instance=instance,
        )
    deleted = run(["schtasks", "/Delete", "/TN", task_name, "/F"])
    if deleted.returncode != 0:
        return _fail(
            instance, f"autostart: schtasks delete failed: {deleted.stderr or deleted.stdout}"
        )
    return CommandResult(
        ok=True,
        message=f"autostart disabled (Task Scheduler task '{task_name}' removed)",
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
    unit_path.write_text(_systemd_unit(instance, python_executable, repo_root), encoding="utf-8")

    reloaded = run(["systemctl", "--user", "daemon-reload"])
    if reloaded.returncode != 0:
        return _Step(False, f"autostart: systemctl daemon-reload failed: {reloaded.stderr}")
    enabled = run(["systemctl", "--user", "enable", "--now", f"{service_name}.service"])
    if enabled.returncode != 0:
        return _Step(False, f"autostart: systemctl enable failed: {enabled.stderr}")
    # Login lingering lets the user service run at boot without an active login;
    # best-effort, since it can require privileges the user may not have.
    run(["loginctl", "enable-linger"])
    return _Step(True, f"systemd user unit '{service_name}'")


def _linux_disable(
    instance: ServerInstance, run: Runner, *, service_name: str, unit_dir: Path | None
) -> CommandResult:
    run(["systemctl", "--user", "disable", f"{service_name}.service"])
    units = unit_dir or _systemd_user_dir()
    unit_path = units / f"{service_name}.service"
    unit_path.unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    return CommandResult(
        ok=True,
        message=f"autostart disabled (systemd user unit '{service_name}' removed)",
        instance=instance,
    )


def _systemd_unit(instance: ServerInstance, python_executable: str, repo_root: Path) -> str:
    # KillMode=process so an agent-triggered `vbot server restart` (which replaces
    # the process with a detached one in the same cgroup) is not killed with the unit.
    return (
        "[Unit]\n"
        "Description=vBot server\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={repo_root}\n"
        f"ExecStart={python_executable} -m server.main --host {instance.host} "
        f"--port {instance.port} --data-dir {instance.data_dir}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "KillMode=process\n"
        "TimeoutStopSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _resolve_vbot_path() -> str | None:
    found = shutil.which("vbot")
    if found:
        return found
    import sysconfig

    scripts_dir = Path(sysconfig.get_path("scripts"))
    for name in ("vbot.exe", "vbot.cmd", "vbot"):
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_runner(command: list[str]) -> CommandRun:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return CommandRun(
            returncode=124,
            stdout="",
            stderr=f"command timed out after {_COMMAND_TIMEOUT_SECONDS:.0f}s: {' '.join(command)}",
        )
    return CommandRun(
        returncode=completed.returncode,
        stdout=(completed.stdout or "").strip(),
        stderr=(completed.stderr or "").strip(),
    )


def _fail(instance: ServerInstance, message: str) -> CommandResult:
    return CommandResult(ok=False, message=message, instance=instance)
