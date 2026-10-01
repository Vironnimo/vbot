"""OS integration owned by one packaged per-user application installation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli.application import processes
from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    ensure_not_removing,
    exclusive,
    operations,
    read_json,
    write_json,
)
from cli.autostart_management import (
    Runner,
    _default_runner,
    _windows_task_command,
)
from cli.server_management import ServerState
from core.utils.server_control import process_started


def refresh_gui_entrypoints(install: Installation) -> None:
    """Publish the protocol-1 GUI companion and repair only our Desktop shortcut.

    Called under the installation operation lock after payload verification.
    Older protocol-1 payloads may predate the additive GUI companion.
    """
    source = install.version() / "runtime" / "vBot.GUI.exe"
    if not source.is_file():
        return
    launcher = contained(install.root, "vBot.GUI.exe")
    changed = []
    if not launcher.exists():
        temporary = contained(install.root, f".gui-{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, launcher)
        finally:
            temporary.unlink(missing_ok=True)
        changed.append("gui_launcher")
    if sys.platform == "win32" and install.install_shape != "server":
        appdata = os.environ.get("APPDATA")
        if appdata:
            shortcut = Path(appdata) / "Microsoft/Windows/Start Menu/Programs/vBot/vBot Desktop.lnk"
            if shortcut.is_file():
                import pythoncom  # type: ignore[import-untyped]
                from win32com.client import Dispatch  # type: ignore[import-untyped]

                pythoncom.CoInitialize()
                try:
                    link = Dispatch("WScript.Shell").CreateShortcut(str(shortcut))
                    # Preserve foreign targets and user-customized arguments.
                    if (
                        Path(link.TargetPath).resolve() == (install.root / "vBot.exe").resolve()
                        and link.Arguments.strip() == "desktop"
                    ):
                        link.TargetPath = str(launcher)
                        link.Save()
                        changed.append("desktop_shortcut")
                finally:
                    link = None  # Release the COM interface before uninitializing its apartment.
                    pythoncom.CoUninitialize()
    if changed:
        logging.getLogger("vbot.application.integration").info(
            "Application GUI entrypoints refreshed (root=%s fields=%s)",
            install.root,
            ",".join(changed),
        )


AUTOSTART_ACTIONS = frozenset({"status", "enable", "disable"})


def task_name(install: Installation) -> str:
    """Return a stable Task Scheduler name scoped to the exact install root."""
    identity = hashlib.sha256(os.path.normcase(str(install.root)).encode("utf-8")).hexdigest()[:12]
    return f"vBot Application {identity}"


def _task_lookup(run: Runner, name: str) -> tuple[bool, str, str]:
    result = run(_windows_task_command("inspect", task_name=name))
    if result.returncode == 3:
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


def _same_path(left: str | Path, right: str | Path) -> bool:
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
) -> dict[str, Any]:
    """Inspect or mutate the exact per-user packaged logon registration."""
    if action not in AUTOSTART_ACTIONS:
        raise ApplicationError("Unknown application Autostart action")
    if action == "enable":
        ensure_not_removing(install.root)
    if platform != "win32":
        raise ApplicationError("Packaged application Autostart is supported only on Windows")
    run = runner or _default_runner
    name = task_name(install)
    launcher = install.root / "vBot.exe"
    if not launcher.is_file():
        raise ApplicationError("Application bootstrap is missing")
    exists, registered_launcher, arguments = _task_lookup(run, name)
    owned = exists and _same_path(registered_launcher, launcher) and arguments == ""
    if exists and not owned:
        raise ApplicationError(f"Task Scheduler entry '{name}' belongs to another application")
    if action == "status":
        return {"ok": True, "enabled": owned, "task_name": name, "launcher": str(launcher)}
    if action == "disable":
        if not exists:
            return {"ok": True, "enabled": False, "task_name": name, "changed": False}
        result = run(_windows_task_command("delete", task_name=name))
        if result.returncode not in {0, 3}:
            raise ApplicationError(
                f"Could not disable application Autostart: {result.stdout or result.stderr}"
            )
        logging.getLogger("vbot.application.integration").info(
            "Application Autostart disabled (mode=task_scheduler)"
        )
        return {"ok": True, "enabled": False, "task_name": name, "changed": True}
    result = run(
        _windows_task_command(
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
    if not verified or not _same_path(verified_launcher, launcher) or verified_arguments:
        raise ApplicationError("Application Autostart registration could not be verified")
    if not owned:
        logging.getLogger("vbot.application.integration").info(
            "Application Autostart enabled (mode=task_scheduler)"
        )
    return {"ok": True, "enabled": True, "task_name": name, "changed": not owned}


# The tray's toasts carry this identity. Its registry entry names them "vBot"
# with the application icon; the tray of the last started installation owns it.
NOTIFICATION_APP_ID = "vBot.Tray"
_NOTIFICATION_IDENTITY_KEY = rf"Software\Classes\AppUserModelId\{NOTIFICATION_APP_ID}"


def register_notification_identity(icon_path: Path) -> None:
    """Name the tray's toasts after vBot instead of the host executable."""
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _NOTIFICATION_IDENTITY_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "vBot")
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path))


def remove_notification_identity(install: Installation) -> None:
    """Remove the toast identity only while it still points into this installation."""
    if sys.platform != "win32":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _NOTIFICATION_IDENTITY_KEY) as key:
            icon, _kind = winreg.QueryValueEx(key, "IconUri")
        owned = Path(str(icon)).resolve().is_relative_to(install.root.resolve())
    except (OSError, ValueError):
        return
    if owned:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _NOTIFICATION_IDENTITY_KEY)
        except OSError:
            logging.getLogger("vbot.application.integration").warning(
                "Could not remove the vBot notification identity", exc_info=True
            )


def registered_uninstaller(root: Path) -> Path:
    """Resolve exactly one complete Inno Setup uninstaller registration."""
    executables = sorted(root.glob("unins[0-9][0-9][0-9].exe"))
    complete = [path for path in executables if path.with_suffix(".dat").is_file()]
    if len(complete) != 1:
        raise ApplicationError("The packaged application uninstaller is missing or ambiguous")
    return complete[0]


def request_host_exit(
    install: Installation,
    *,
    timeout: float = 15,
    monotonic: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Request graceful tray exit and wait for the exact recorded process."""
    state_path = install.root / "host.json"
    if not state_path.is_file():
        return {"ok": True, "running": False, "changed": False}
    state = read_json(state_path, limit=4096)
    if (
        state.get("schema_version") != 1
        or type(state.get("pid")) is not int
        or not isinstance(state.get("process_created"), (int, float))
    ):
        raise ApplicationError("Application host ownership record is invalid")
    pid = state["pid"]
    created = float(state["process_created"])
    try:
        import psutil  # type: ignore[import-untyped]

        process = psutil.Process(pid)
        if abs(process_started(process) - created) > 0.01:
            raise ApplicationError("Application host ownership record is stale")
        if not _same_path(process.exe(), install.root / "vBot.exe"):
            raise ApplicationError("Application host ownership record targets another executable")
    except psutil.NoSuchProcess:
        state_path.unlink(missing_ok=True)
        return {"ok": True, "running": False, "changed": False}
    request = install.root / "host-exit-request.json"
    write_json(request, {"schema_version": 1, "nonce": uuid.uuid4().hex})
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return {"ok": True, "running": False, "changed": True}
        pause(0.1)
    raise ApplicationError("The vBot tray did not exit in time; application removal was cancelled")


UninstallerLauncher = Callable[[Path], None]
RemoveTree = Callable[[Path], None]


def _launch_uninstaller(path: Path) -> None:
    subprocess.Popen(
        [str(path), "/CURRENTUSER", "/SILENT"],
        cwd=path.parent,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path)


def uninstall(
    install: Installation,
    *,
    remove_data: bool = False,
    data_only: bool = False,
    platform: str = sys.platform,
    runner: Runner | None = None,
    stop: Callable[[Installation], Any] = processes.stop,
    start: Callable[[Installation], Any] = processes.start,
    server_state: Callable[[Installation], ServerState] = processes.server_state,
    exit_host: Callable[[Installation], Any] = request_host_exit,
    launcher: UninstallerLauncher = _launch_uninstaller,
    remove_tree: RemoveTree = _remove_tree,
) -> dict[str, Any]:
    """Stop the exact owned server, optionally remove data, then launch Inno removal."""
    if platform != "win32":
        raise ApplicationError("Packaged application removal is supported only on Windows")
    if remove_data and data_only:
        raise ApplicationError("Choose application-and-data removal or data-only reset")
    uninstaller = None if data_only else registered_uninstaller(install.root)
    if Path.cwd().resolve().is_relative_to(install.root.resolve()):
        raise ApplicationError("Change to a directory outside the application before uninstalling")
    # Keep dispatch closed from the pending-operation check through removal. The
    # tray exit happens before taking the operation lock because tray shutdown
    # may need that lock to stop its server.
    with exclusive(install.root, "dispatch", timeout=5):
        pending = [item for item in operations(install) if not item.terminal]
        if pending:
            raise ApplicationError(
                f"Update {pending[0].id} is still active; wait for it before removing vBot"
            )
        if not data_only:
            exit_host(install)
        with exclusive(install.root, "operation", timeout=5):
            was_running = False
            if install.owns_server and data_only:
                # A reset keeps the server's prior state, so a busy server must never
                # be taken for a stopped one and have its data deleted underneath it.
                state = server_state(install)
                if state == "unresponsive":
                    raise ApplicationError(
                        "The server is running but does not answer its health check, so "
                        "vBot data was not reset; try again once the server responds"
                    )
                if state == "foreign":
                    raise ApplicationError(
                        "The server port is held by another process or application version, "
                        "so vBot data was not reset"
                    )
                was_running = state == "running"
            if install.owns_server and (was_running or not data_only):
                result = stop(install)
                if not result.ok:
                    raise ApplicationError(
                        "Application removal aborted because the server could not stop: "
                        f"{result.message}"
                    )
            removed_data = False
            if remove_data or data_only:
                if not install.owns_server or install.server_data_directory is None:
                    raise ApplicationError("This Desktop Client does not own server data")
                data = Path(install.server_data_directory).resolve()
                home = Path.home().resolve()
                root = install.root.resolve()
                cwd = Path.cwd().resolve()
                if (
                    data in {data.parent, home, root}
                    or data.is_relative_to(root)
                    or root.is_relative_to(data)
                    or cwd.is_relative_to(data)
                ):
                    raise ApplicationError(
                        "Refusing to remove an unsafe application data directory"
                    )
                if data.exists():
                    remove_tree(data)
                    removed_data = True
            if data_only:
                restarted = False
                if was_running:
                    result = start(install)
                    if not result.ok:
                        raise ApplicationError(
                            "Application data was reset, but the server could not restart: "
                            f"{result.message}"
                        )
                    restarted = True
                return {
                    "ok": True,
                    "completed": True,
                    "application_preserved": True,
                    "autostart_preserved": True,
                    "data_removed": removed_data,
                    "server_restarted": restarted,
                }
            autostart(install, "disable", platform=platform, runner=runner)
            assert uninstaller is not None
            launcher(uninstaller)
            return {
                "ok": True,
                "accepted": True,
                "launched": True,
                "uninstaller": str(uninstaller),
                "data_removed": removed_data,
                "data_preserved": not remove_data,
            }


def begin_removal(install: Installation) -> dict[str, Any]:
    """Reserve actual Inno deletion after its normal stop/exit preflight."""
    import psutil  # type: ignore[import-untyped]

    with exclusive(install.root, "dispatch", timeout=15), exclusive(install.root):
        if any(not operation.terminal for operation in operations(install)):
            raise ApplicationError("An update is still pending; wait before removing vBot")
        if install.owns_server:
            # Removal refuses and never stops: `server stop` ran before this step.
            state = processes.server_state(install)
            if state == "running":
                raise ApplicationError("The application server must stop before removal")
            if state == "unresponsive":
                raise ApplicationError(
                    "The application server is still running but does not answer its "
                    "health check; it must stop before removal"
                )
            if state == "foreign":
                raise ApplicationError(
                    "The server port is held by another process or application version; "
                    "it must be freed before removal"
                )
        uninstaller = registered_uninstaller(install.root)
        parent = psutil.Process(os.getppid())
        try:
            second_phase = Path(parent.exe())
            second_phase_arguments = parent.cmdline()
            first_phase = parent.parent()
            second_phase_targets = [
                argument.partition("=")[2]
                for argument in second_phase_arguments
                if argument.upper().startswith("/SECONDPHASE=")
            ]
            owned_uninstaller = (
                second_phase.name.casefold() == "_unins.tmp"
                and len(second_phase_targets) == 1
                and _same_path(Path(second_phase_targets[0]).resolve(), uninstaller.resolve())
                and first_phase is not None
                and _same_path(Path(first_phase.exe()).resolve(), uninstaller.resolve())
            )
        except (OSError, psutil.Error):
            owned_uninstaller = False
            first_phase = None
        if not owned_uninstaller or first_phase is None:
            raise ApplicationError(
                "Application removal must be started by the registered vBot uninstaller"
            )
        exempt = {os.getpid(), parent.pid, first_phase.pid}
        for process in psutil.process_iter(["pid", "exe"]):
            executable = process.info.get("exe")
            executable_path = Path(executable) if executable else None
            if (
                process.pid not in exempt
                and executable_path is not None
                and executable_path.is_absolute()
                and executable_path.resolve().is_relative_to(install.root.resolve())
            ):
                raise ApplicationError(
                    "Close remaining vBot Desktop windows and commands before removal: "
                    f"{executable} (PID {process.pid})"
                )
        write_json(
            contained(install.root, "removal-pending.json"),
            {"schema_version": 1, "pid": parent.pid, "process_created": process_started(parent)},
        )
        # The tray has exited; if removal is cancelled, its next start registers again.
        remove_notification_identity(install)
    return {"ok": True, "removal_pending": True}


def reset_removal(install: Installation) -> dict[str, Any]:
    """Explicitly recover a complete installation after its remover has died."""
    import math

    import psutil  # type: ignore[import-untyped]

    from cli.application.packages import validate_release

    with (
        exclusive(install.root, "dispatch", timeout=5),
        exclusive(install.root, allow_removal=True),
    ):
        path = contained(install.root, "removal-pending.json")
        if not path.exists():
            return {"ok": True, "changed": False}
        record = read_json(path, limit=4096)
        created = record.get("process_created")
        if (
            record.get("schema_version") != 1
            or type(record.get("pid")) is not int
            or record["pid"] <= 0
            or not isinstance(created, (int, float))
            or isinstance(created, bool)
            or not math.isfinite(created)
        ):
            raise ApplicationError("Removal ownership is invalid; preserve it for inspection")
        try:
            process = psutil.Process(record["pid"])
            if abs(process_started(process) - created) < 0.001 and process.is_running():
                raise ApplicationError("The uninstaller is still running")
        except psutil.NoSuchProcess:
            pass
        validate_release(
            install.version(), shape=install.install_shape, remove_bytecode_caches=True
        )
        path.unlink()
    return {"ok": True, "changed": True, "removal_pending": False}
