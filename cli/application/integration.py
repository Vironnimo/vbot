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
from dataclasses import dataclass
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
    DEFAULT_TASK_NAME,
    Runner,
    _default_runner,
    _windows_task_command,
    autostart_status,
    disable_autostart,
)
from cli.install_state import InstallState, read_install_state
from cli.server_management import probe_health, resolve_instance, stop_server
from core.utils.processes import subprocess_creation_flags


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
            "Refreshed application GUI entrypoints: root=%s fields=%s", install.root, changed
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
    return {"ok": True, "enabled": True, "task_name": name, "changed": not owned}


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
        if abs(process.create_time() - created) > 0.01:
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
    probe: Callable[[Any], Any] = probe_health,
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
            if install.owns_server:
                was_running = bool(probe(processes.target(install)).is_vbot) if data_only else False
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


@dataclass(frozen=True)
class CheckoutTransition:
    """Verified source-install facts retained until packaged registration succeeds."""

    state: InstallState
    old_autostart_enabled: bool = False
    old_autostart_launcher: str | None = None


GitRunner = Callable[[Path], subprocess.CompletedProcess[str]]


def _git_status(checkout: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=subprocess_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApplicationError("Could not inspect the source checkout before transition") from exc


def prepare_checkout_transition(
    checkout: Path,
    *,
    shape: str,
    platform: str = sys.platform,
    git_runner: GitRunner = _git_status,
    runner: Runner | None = None,
    stop: Callable[[Any], Any] = stop_server,
) -> CheckoutTransition:
    """Refuse local changes and stop the exact source-owned server before cutover."""
    checkout = checkout.resolve()
    state = read_install_state(checkout)
    if state is None or state.install_shape != shape:
        raise ApplicationError("The existing checkout has no matching installation manifest")
    status = git_runner(checkout)
    if status.returncode != 0:
        raise ApplicationError("Could not inspect the source checkout before transition")
    if status.stdout.strip():
        raise ApplicationError(
            "The source checkout has local changes; use 'vbot customize prepare' to migrate them"
        )
    if shape == "desktop-client":
        return CheckoutTransition(state)
    if not state.server_host or not state.server_port or not state.server_data_directory:
        raise ApplicationError("The source installation has no exact server target")
    instance = resolve_instance(
        host=state.server_host,
        port=state.server_port,
        data_dir=state.server_data_directory,
    )
    stopped = stop(instance)
    if not stopped.ok:
        raise ApplicationError(
            f"Source transition aborted because the server could not stop: {stopped.message}"
        )
    old_launcher = str(Path(state.python_executable).parent / "vbot-autostart.exe")
    enabled = False
    if platform == "win32":
        existing = autostart_status(
            instance,
            platform=platform,
            runner=runner,
            task_name=DEFAULT_TASK_NAME,
            windows_launcher_path=old_launcher,
        )
        if not existing.ok:
            raise ApplicationError(
                f"Could not verify source Autostart ownership: {existing.message}"
            )
        enabled = existing.message.startswith("autostart: enabled ")
    return CheckoutTransition(state, enabled, old_launcher)


def finish_checkout_transition(
    install: Installation,
    transition: CheckoutTransition,
    *,
    platform: str = sys.platform,
    runner: Runner | None = None,
) -> None:
    """Move owned logon registration only after packaged registration verifies."""
    if not transition.old_autostart_enabled:
        return
    autostart(install, "enable", platform=platform, runner=runner)
    state = transition.state
    assert state.server_host and state.server_port and state.server_data_directory
    instance = resolve_instance(
        host=state.server_host, port=state.server_port, data_dir=state.server_data_directory
    )
    status = autostart_status(
        instance,
        platform=platform,
        runner=runner,
        task_name=DEFAULT_TASK_NAME,
        windows_launcher_path=transition.old_autostart_launcher,
    )
    if not status.ok or not status.message.startswith("autostart: enabled "):
        raise ApplicationError("Source Autostart ownership changed during transition")
    removed = disable_autostart(
        instance, platform=platform, runner=runner, task_name=DEFAULT_TASK_NAME
    )
    if not removed.ok:
        raise ApplicationError(f"Could not retire source Autostart: {removed.message}")


def begin_removal(install: Installation) -> dict[str, Any]:
    """Reserve actual Inno deletion after its normal stop/exit preflight."""
    import psutil  # type: ignore[import-untyped]

    with exclusive(install.root, "dispatch", timeout=15), exclusive(install.root):
        if any(not operation.terminal for operation in operations(install)):
            raise ApplicationError("An update is still pending; wait before removing vBot")
        if install.owns_server and probe_health(processes.target(install)).reachable:
            raise ApplicationError("The application server must stop before removal")
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
            {"schema_version": 1, "pid": parent.pid, "process_created": parent.create_time()},
        )
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
            if abs(process.create_time() - created) < 0.001 and process.is_running():
                raise ApplicationError("The uninstaller is still running")
        except psutil.NoSuchProcess:
            pass
        validate_release(
            install.version(), shape=install.install_shape, remove_bytecode_caches=True
        )
        path.unlink()
    return {"ok": True, "changed": True, "removal_pending": False}
