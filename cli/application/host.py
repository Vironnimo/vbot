"""Tray-facing application facade for one packaged vBot installation."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

import psutil  # type: ignore[import-untyped]

from cli.application import operations, processes
from cli.application.operations import child_environment
from cli.application.state import (
    ApplicationError,
    Installation,
    contained,
    discover,
    exclusive,
    read_json,
    write_json,
)
from cli.application.tray import TrayState, run_tray
from core.utils.logging import LogManager
from core.utils.processes import subprocess_creation_flags

_LOGGER = logging.getLogger("vbot.application.host")


class ApplicationFacade:
    """Translate the tray's small action surface to installed application owners."""

    def __init__(self, install: Installation) -> None:
        self._install = install
        self._status_error = ""
        self._display_version_id = ""
        self._display_version = ""

    def state(self) -> TrayState:
        try:
            operation = operations.status(self._install)
        except Exception as error:
            self.report_error(f"Could not read update status: {error}")
            operation = None
        if not self._install.owns_server:
            server_state = "not_applicable"
        else:
            health = processes.probe_health(processes.target(self._install))
            server_state = (
                "running" if health.is_vbot else "stopped" if not health.reachable else "conflict"
            )
        active = self._install.version()
        if active.name != self._display_version_id:
            release = read_json(active / "release.json", limit=32 * 1024**2)
            version = release.get("version")
            self._display_version = version if isinstance(version, str) else ""
            self._display_version_id = active.name
        return TrayState(
            server_state=server_state,
            install_shape=self._install.install_shape,
            update_phase=operation.phase if operation else None,
            update_message=operation.message if operation else "",
            version=self._display_version,
            exit_requested=_valid_exit_request(self._install.root),
            error=self._status_error,
        )

    def start_server(self) -> None:
        self._require_server()
        with exclusive(self._install.root, "operation"):
            self._require_success(processes.start(self._install))
        self._clear_status_error()

    def stop_server(self) -> None:
        self._require_server()
        with exclusive(self._install.root, "operation", allow_removal=True):
            self._require_success(processes.stop(self._install))
        self._clear_status_error()

    def restart_server(self) -> None:
        self._require_server()
        with exclusive(self._install.root, "operation"):
            self._require_success(processes.stop(self._install))
            self._require_success(processes.start(self._install))
        self._clear_status_error()

    def open_desktop(self, *, host: str | None = None, port: int | None = None) -> None:
        from cli.application.state import ensure_not_removing

        # Serialize launch with the uninstaller's removal reservation.
        with exclusive(self._install.root, "dispatch", timeout=5):
            ensure_not_removing(self._install.root)
            if self._install.install_shape not in {"server-desktop", "desktop-client"}:
                raise ApplicationError("This installation does not include the Desktop app")
            arguments = [str(self._install.interpreter(role="Desktop")), "-m", "desktop.main"]
            if host is not None or port is not None:
                if host is not None:
                    arguments.extend(("--host", host))
                if port is not None:
                    arguments.extend(("--port", str(port)))
            elif self._install.owns_server:
                assert (
                    self._install.server_host is not None and self._install.server_port is not None
                )
                arguments.extend(
                    ("--host", self._install.server_host, "--port", str(self._install.server_port))
                )
            subprocess.Popen(
                arguments,
                cwd=self._install.version() / "app",
                env=child_environment(self._install),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess_creation_flags(new_process_group=True, breakaway=True),
                start_new_session=os.name != "nt",
            )

    def open_browser(self) -> None:
        self._require_server()
        webbrowser.open(processes.target(self._install).url)

    def start_update(self) -> None:
        operations.request_update(self._install)

    def open_logs(self) -> None:
        path = self._install.root / "logs"
        path.mkdir(parents=True, exist_ok=True)
        _open_folder(path)

    def open_server_logs(self) -> None:
        self._require_server()
        path = processes.target(self._install).data_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        _open_folder(path)

    def show_update(self) -> None:
        operation = operations.status(self._install)
        if operation is None:
            message = "No update has been requested."
        else:
            message = operation.message
            if operation.target_label:
                message += (
                    f"\n\nVersion: {operation.previous_label or 'unknown'} "
                    f"-> {operation.target_label}"
                )
            if operation.error:
                message += f"\n\n{operation.error}"
            message += f"\n\nDetails in the terminal:\nvbot update status {operation.id}"
            if operation.phase == "prepared":
                message += (
                    f"\n\nActivate this prepared version:\nvbot update activate {operation.id}"
                )
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "vBot update", 0x40)

    def quit(self) -> None:
        if self._install.owns_server:
            self.stop_server()

    def report_error(self, message: str) -> None:
        """Expose a recoverable host failure through the next tray state."""

        self._status_error = message

    def _require_server(self) -> None:
        if not self._install.owns_server:
            raise ApplicationError("This Desktop Client installation has no local server")

    @staticmethod
    def _require_success(result: object) -> None:
        if getattr(result, "ok", False) is not True:
            raise ApplicationError(str(getattr(result, "message", "Application action failed")))

    def _clear_status_error(self) -> None:
        self._status_error = ""


def main() -> int:
    """Run at most one tray host for the explicit packaged installation."""

    install = discover()
    if install is None:
        return 0
    try:
        with exclusive(install.root, "host"):
            contained(install.root, "host-exit-request.json").unlink(missing_ok=True)
            write_json(
                contained(install.root, "host.json"),
                {
                    "schema_version": 1,
                    "pid": os.getpid(),
                    "process_created": psutil.Process().create_time(),
                },
            )
            manager = LogManager(data_dir=install.root, enable_console=False)
            try:
                facade = ApplicationFacade(install)
                try:
                    operations.recover_operations(install)
                    operation = operations.status(install)
                    if install.owns_server and (operation is None or operation.terminal):
                        facade.start_server()
                except Exception as error:
                    _LOGGER.exception(
                        "Could not recover or start the packaged application", exc_info=error
                    )
                    facade.report_error(f"Startup failed: {error}")
                run_tray(facade, install.version() / "app" / "desktop" / "icon.ico")
            finally:
                manager.close()
                contained(install.root, "host.json").unlink(missing_ok=True)
                contained(install.root, "host-exit-request.json").unlink(missing_ok=True)
    except ApplicationError as error:
        if str(error) != "Another application operation is running":
            raise
    return 0


def _open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    subprocess.Popen(
        ["xdg-open", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _valid_exit_request(root: Path) -> bool:
    """Accept only the bounded request record written by the exact host-exit flow."""

    path = contained(root, "host-exit-request.json")
    if not path.is_file():
        return False
    try:
        request = read_json(path, limit=4096)
    except ApplicationError:
        _LOGGER.warning("Ignoring invalid application host exit request")
        return False
    nonce = request.get("nonce")
    if (
        request.get("schema_version") != 1
        or not isinstance(nonce, str)
        or len(nonce) != 32
        or any(character not in "0123456789abcdef" for character in nonce)
    ):
        _LOGGER.warning("Ignoring invalid application host exit request")
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
