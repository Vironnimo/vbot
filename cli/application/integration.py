"""OS integration owned by one packaged per-user application installation."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from cli.application import processes
from cli.application.autostart import Runner, autostart, same_path
from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    exclusive,
    operations,
    read_json,
    write_json,
)
from cli.server_management import ServerState
from core.utils.file_status import exists_strict
from core.utils.server_control import live_server_ports, process_started

_LOGGER = logging.getLogger("vbot.application.integration")


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
    except OSError, ValueError:
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
        if not same_path(process.exe(), install.root / "vBot.exe"):
            raise ApplicationError("Application host ownership record targets another executable")
    except psutil.NoSuchProcess:
        state_path.unlink(missing_ok=True)
        return {"ok": True, "running": False, "changed": False}
    request = install.root / "host-exit-request.json"
    write_json(request, {"schema_version": 1, "nonce": uuid.uuid4().hex})
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            exited = not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            exited = True
        if exited:
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


def _normalized_removal_installation(install: Installation) -> Installation:
    """Resolve removal targets and refuse overlapping application and server data."""
    root = install.root.expanduser().resolve()
    data = (
        Path(install.server_data_directory).expanduser().resolve()
        if install.owns_server and install.server_data_directory is not None
        else None
    )
    if data is not None and (data.is_relative_to(root) or root.is_relative_to(data)):
        raise ApplicationError("Application and server data directories must be separate")
    return replace(
        install,
        root=root,
        server_data_directory=str(data) if data is not None else install.server_data_directory,
    )


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
    command_link: Path | None = None,
) -> dict[str, Any]:
    """Stop the exact owned server, optionally remove data, then remove the application.

    Windows hands the application removal to its registered Inno uninstaller.
    Linux removes the logon unit, the ``vbot`` command link and the
    installation directory itself.
    """
    if platform != "win32" and not platform.startswith("linux"):
        raise ApplicationError(f"Packaged application removal is not supported on {platform}")
    if remove_data and data_only:
        raise ApplicationError("Choose application-and-data removal or data-only reset")
    # Check before locks, process stops or uninstaller launch: application-only
    # removal must not erase data recorded inside the application either.
    install = _normalized_removal_installation(install)
    data = (
        Path(install.server_data_directory)
        if install.owns_server and install.server_data_directory is not None
        else None
    )
    if remove_data or data_only:
        if data is None:
            raise ApplicationError("This Desktop Client does not own server data")
        if data in {data.parent, Path.home().resolve()} or Path.cwd().resolve().is_relative_to(
            data
        ):
            raise ApplicationError("Refusing to remove an unsafe application data directory")
    uninstaller = (
        registered_uninstaller(install.root) if platform == "win32" and not data_only else None
    )
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
                assert data is not None  # Validated before any changes above.
                try:
                    claims = live_server_ports(data)
                except OSError as exc:
                    raise ApplicationError(
                        "Application data removal aborted: server claims could not be checked"
                    ) from exc
                if claims:
                    raise ApplicationError(
                        f"Application data is still in use by a server on ports: {claims}"
                    )
                if exists_strict(data):
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
            if platform == "win32" or install.owns_server:
                autostart(install, "disable", platform=platform, runner=runner)
            if uninstaller is None:
                link = command_link or Path.home() / ".local" / "bin" / "vbot"
                if link.is_symlink() and same_path(link, install.bootstrap):
                    link.unlink()
                # Logged first: the installation's own log goes with it.
                _LOGGER.info("Application removal started (root=%s)", install.root)
                remove_tree(install.root)
                return {
                    "ok": True,
                    "completed": True,
                    "removed": True,
                    "data_removed": removed_data,
                    "data_preserved": not remove_data,
                }
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

    install = _normalized_removal_installation(install)
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
                and same_path(Path(second_phase_targets[0]).resolve(), uninstaller.resolve())
                and first_phase is not None
                and same_path(Path(first_phase.exe()).resolve(), uninstaller.resolve())
            )
        except OSError, psutil.Error:
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
