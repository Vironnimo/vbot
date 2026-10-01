"""Tray-facing application facade for one packaged vBot installation."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from types import EllipsisType
from typing import Any
from urllib.parse import urlencode

import psutil  # type: ignore[import-untyped]

from cli.application import operations, processes
from cli.application.monitor import MonitorStatus, ServerMonitor
from cli.application.notifications import Notifier
from cli.application.state import (
    ApplicationError,
    Installation,
    Operation,
    contained,
    discover,
    exclusive,
    loaded_version_id,
    read_json,
    write_json,
)
from cli.application.tray import TraySink, TrayState, run_tray
from cli.server_management import HealthProbeResult, ServerInstance, ServerState, classify_server
from core.utils.logging import LogManager
from core.utils.processes import subprocess_creation_flags
from core.utils.server_control import process_started

_LOGGER = logging.getLogger("vbot.application.host")
_ACTIVITY_LIMIT = 200
_FAILED_UPDATE_PHASES = frozenset({"failed", "rolled_back", "needs_attention"})
#: How long a successor must survive before the handoff counts as started.
_SUCCESSOR_GRACE_SECONDS = 2.0
#: How long a successor waits for its predecessor to release the host lock.
_SUCCESSOR_LOCK_SECONDS = 30.0


class ApplicationFacade:
    """Translate the tray's small action surface to installed application owners.

    ``state`` stays cheap enough to call every second: update records are
    reread only when their files change, and the server state comes from the
    event-stream monitor that :meth:`watch` starts.

    ``running_version`` names the installed version whose code this tray runs,
    by default derived from the loaded module; ``None`` (a source checkout)
    disables restarts into a newly activated version.
    """

    def __init__(
        self, install: Installation, *, running_version: str | None | EllipsisType = ...
    ) -> None:
        self._install = install
        self._running_version = (
            loaded_version_id(Path(__file__))
            if isinstance(running_version, EllipsisType)
            else running_version
        )
        self._status_error = ""
        self._active_version_id = ""
        self._display_version = ""
        self._version_signature: tuple[int, int] | None = None
        self._observer = operations.OperationObserver(install)
        self._server_target_value: ServerInstance | None = None
        self._source_label: str | None = None
        self._monitor: ServerMonitor | None = None
        self._notifier: Notifier | None = None
        self._progress_id: str | None = None
        self._progress_key: tuple[str, str, str | None] | None = None
        self._progress_announced = ""
        self._progress_history = False
        self._observed = False
        self._activity: list[str] = []
        self._last_update = ""

    def state(self) -> TrayState:
        try:
            operation = self._observer.latest()
            update_idle = operation is None or operation.terminal
        except Exception as error:
            self.report_error(f"Could not read update status: {error}")
            operation, update_idle = None, False
        activity = self._observe_progress(operation)
        server_state, server_url = self._server()
        version = self._version()
        return TrayState(
            server_state=server_state,
            install_shape=self._install.install_shape,
            update_phase=operation.phase if operation else None,
            update_message=operation.message if operation else "",
            version=version,
            exit_requested=_valid_exit_request(self._install.root),
            error=self._status_error,
            server_url=server_url,
            update_activity=activity,
            details=self._details(server_url),
            restart_pending=update_idle and self._activation_changed(),
        )

    def watch(self, sink: TraySink) -> None:
        """Follow the server's event stream and report changes and toasts to ``sink``."""

        monitor: ServerMonitor | None = None

        async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
            return None if monitor is None else await monitor.rpc(method, params)

        notifier = Notifier(
            sink,
            rpc,
            owns_server=self._install.owns_server,
            update_active=self._update_running,
        )
        monitor = ServerMonitor(
            self._monitor_target,
            _MonitorEvents(notifier, sink),
            local=self._install.owns_server,
            user_agent=f"vBot-Tray/{self._version() or 'unknown'}",
            classify=self._classify_server if self._install.owns_server else None,
        )
        self._notifier, self._monitor = notifier, monitor
        monitor.start()

    def unwatch(self) -> None:
        if self._monitor is not None:
            self._monitor.close()

    def start_server(self) -> None:
        self._require_server()
        with exclusive(self._install.root, "operation"):
            self._require_success(processes.start(self._install))
        self._clear_status_error()
        self._reconnect()

    def stop_server(self, *, initiator: str = "tray_stop") -> None:
        self._require_server()
        self._expect_stop()
        with exclusive(self._install.root, "operation", allow_removal=True):
            self._require_success(processes.stop(self._install, initiator=initiator))
        self._clear_status_error()

    def restart_server(self) -> None:
        self._require_server()
        self._expect_stop()
        with exclusive(self._install.root, "operation"):
            self._require_success(processes.stop(self._install, initiator="tray_restart"))
            self._require_success(processes.start(self._install))
        self._clear_status_error()
        self._reconnect()

    def open_desktop(self, *, host: str | None = None, port: int | None = None) -> None:
        from cli.application.desktop import open_desktop

        open_desktop(self._install, host=host, port=port)

    def open_browser(self) -> None:
        self._require_server()
        webbrowser.open(processes.target(self._install).url)

    def open_session(self, agent: str, session: str) -> None:
        """Show one Session in the Desktop, or in the browser without a Desktop."""

        from cli.application.desktop import open_desktop

        if self._install.install_shape == "server":
            query = urlencode({"open_agent": agent, "open_session": session})
            webbrowser.open(f"{processes.target(self._install).url}/?{query}")
        elif self._install.owns_server:
            open_desktop(self._install, open_session=(agent, session))
        else:
            target = _desktop_target()
            if target is None:
                raise ApplicationError("The Desktop has no server to open this Session on")
            open_desktop(
                self._install, host=target[0], port=target[1], open_session=(agent, session)
            )

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

    def restart(self) -> None:
        """Start a successor tray on the active version; the server keeps running.

        The successor is the stable bootstrap, which resolves the active version
        itself and waits for this host to release its lock. Raises
        ``ApplicationError`` when the successor exits at once; this tray then
        keeps running. The caller stops this tray after a successful handoff.
        """

        if self._running_version is None:
            raise ApplicationError("This tray does not run from an installed version")
        self._version()
        process = subprocess.Popen(
            [str(self._install.root / "vBot.exe")],
            cwd=self._install.root,
            env={**operations.child_environment(self._install), operations.HOST_SUCCESSOR_ENV: "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess_creation_flags(new_process_group=True, breakaway=True),
            start_new_session=os.name != "nt",
        )
        try:
            process.wait(timeout=_SUCCESSOR_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise ApplicationError(
                f"The tray for the active version exited at once (exit code {process.returncode})"
            )
        _LOGGER.info(
            "Tray restarting into the active version (from=%s to=%s)",
            self._running_version,
            self._active_version_id,
        )

    def quit(self) -> None:
        if self._install.owns_server:
            self.stop_server(initiator="tray_quit")

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

    def _expect_stop(self) -> None:
        if self._notifier is not None:
            self._notifier.expect_server_stop()

    def _reconnect(self) -> None:
        if self._monitor is not None:
            self._monitor.reconnect()

    def _version(self) -> str:
        # Resolve the verified pointer only when its file changed.
        try:
            stat = (self._install.root / "active-version").stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        if signature is not None and signature == self._version_signature:
            return self._display_version
        self._version_signature = signature
        active = self._install.version()
        if active.name != self._active_version_id:
            release = read_json(active / "release.json", limit=32 * 1024**2)
            version = release.get("version")
            self._display_version = version if isinstance(version, str) else ""
            self._active_version_id = active.name
            self._source_label = None
        return self._display_version

    def _activation_changed(self) -> bool:
        """Whether another version became active than the one this tray runs.

        Compares the active id that :meth:`_version` caches, so it reads no file.
        """

        return (
            self._running_version is not None
            and bool(self._active_version_id)
            and os.path.normcase(self._active_version_id) != os.path.normcase(self._running_version)
        )

    def _server(self) -> tuple[str, str]:
        status = self._monitor.status if self._monitor is not None else MonitorStatus()
        if not self._install.owns_server:
            return "not_applicable", status.url or ""
        if status.connection == "rejected":
            server_state = "running" if status.vbot else "conflict"
        else:
            server_state = {
                "connected": "running",
                "unresponsive": "unresponsive",
                "refused": "stopped",
                "unreachable": "stopped",
            }.get(status.connection, "unknown")
        return server_state, self._server_url()

    def _server_url(self) -> str:
        return self._server_target().url

    def _server_target(self) -> ServerInstance:
        if self._server_target_value is None:
            self._server_target_value = processes.target(self._install)
        return self._server_target_value

    def _classify_server(self, health: HealthProbeResult) -> ServerState:
        """Classify the owned target from the monitor's unanswered ``/health`` request.

        The monitor calls this on its own thread after a failed connect; the
        control record and process check tell a busy server from a stopped one
        without probing the target again.
        """

        return classify_server(self._server_target(), health=health)

    def _monitor_target(self) -> str | None:
        if self._install.owns_server:
            return self._server_url()
        target = _desktop_target()
        if target is None:
            return None
        from cli._server_target import build_server_base_url

        return build_server_base_url(*target)

    def _update_running(self) -> bool:
        try:
            operation = self._observer.latest()
        except Exception:
            return True
        return operation is not None and not operation.terminal

    def _observe_progress(self, operation: Operation | None) -> tuple[str, ...]:
        """Record update progress like a waiting ``vbot update`` prints it."""

        first, self._observed = not self._observed, True
        if operation is None:
            return ()
        if operation.id != self._progress_id:
            self._progress_id, self._progress_key = operation.id, None
            self._progress_announced, self._activity = "", []
            # An update already finished when the tray started is only history.
            self._progress_history = first and operation.terminal
        key = (operation.phase, operation.message, operation.target_label)
        if key == self._progress_key:
            return tuple(self._activity)
        self._progress_key = key
        if operation.terminal:
            self._last_update = (
                f"{operations.result_summary(self._install, operation)} "
                f"({_local_time(operation.updated_at, full=True)})"
            )
            self._source_label = None
        if self._progress_history:
            return ()
        stamp = time.strftime("%H:%M:%S")
        if operation.target_label and operation.target_label != self._progress_announced:
            self._progress_announced = operation.target_label
            before = operation.previous_label
            self._activity.append(
                f"{stamp}  Updating vBot: {before} -> {operation.target_label}"
                if before and before != operation.target_label
                else f"{stamp}  Target version: {operation.target_label}"
            )
        if not operation.terminal:
            self._activity.append(f"{stamp}  {operation.message}")
        else:
            summary = operations.result_summary(self._install, operation)
            self._activity.append(f"{stamp}  {summary}")
            if operation.phase in _FAILED_UPDATE_PHASES:
                if operation.message:
                    self._activity.append(f"{' ' * 10}{operation.message}")
                if operation.error:
                    self._activity.append(f"{' ' * 10}Reason: {operation.error}")
            if self._notifier is not None:
                self._notifier.update_finished(operation, summary)
        del self._activity[:-_ACTIVITY_LIMIT]
        return tuple(self._activity)

    def _details(self, server_url: str) -> tuple[tuple[str, str], ...]:
        rows: list[tuple[str, str]] = []
        if self._install.owns_server:
            rows.append(("Server", server_url))
            rows.append(("Data", str(self._install.server_data_directory or "")))
        else:
            rows.append(("Server", server_url or "No Desktop server selected"))
        rows.append(("Updates from", self._update_source()))
        if self._last_update:
            rows.append(("Last update", self._last_update))
        rows.append(("Installed in", str(self._install.root)))
        return tuple(rows)

    def _update_source(self) -> str:
        if self._source_label is None:
            from cli.application.state import load_installation

            # `vbot application channel` rewrites the record while the tray runs.
            try:
                channel = load_installation(self._install.root).channel
            except ApplicationError as error:
                self._source_label = f"Unreadable installation record: {error}"
            else:
                self._source_label = {
                    "release": "Published releases",
                    "main": "Newest main builds",
                }.get(channel, "A custom release source")
        return self._source_label


class _MonitorEvents:
    """Deliver monitor observations to the notifier and wake the tray on status changes."""

    def __init__(self, notifier: Notifier, sink: TraySink) -> None:
        self._notifier = notifier
        self._sink = sink

    def status_changed(self, status: MonitorStatus) -> None:
        self._notifier.status_changed(status)
        self._sink.changed()

    def connection_lost(self, close_code: int) -> None:
        self._notifier.connection_lost(close_code)

    def event_received(self, event: dict[str, Any]) -> None:
        self._notifier.event_received(event)


def main() -> int:
    """Run at most one tray host for the explicit packaged installation."""

    install = discover()
    if install is None:
        return 0
    # A successor started by a restart waits for its predecessor to exit.
    successor = os.environ.get(operations.HOST_SUCCESSOR_ENV) == "1"
    try:
        with exclusive(install.root, "host", timeout=_SUCCESSOR_LOCK_SECONDS if successor else 0):
            contained(install.root, "host-exit-request.json").unlink(missing_ok=True)
            write_json(
                contained(install.root, "host.json"),
                {
                    "schema_version": 1,
                    "pid": os.getpid(),
                    "process_created": process_started(psutil.Process()),
                },
            )
            manager = LogManager(data_dir=install.root, enable_console=False)
            try:
                facade = ApplicationFacade(install)
                try:
                    operations.recover_operations(install)
                    operation = operations.status(install)
                    # A successor keeps the server as its predecessor left it,
                    # including a server the user stopped on purpose.
                    if (
                        not successor
                        and install.owns_server
                        and (operation is None or operation.terminal)
                    ):
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


def _desktop_target() -> tuple[str, int] | None:
    """Return the server the Desktop last used, which a Desktop Client tray follows."""

    try:
        from desktop.settings import read_last_used
    except ImportError:
        return None
    target = read_last_used()
    if target is None:
        return None
    host, port = target.get("host"), target.get("port")
    return (host, port) if isinstance(host, str) and isinstance(port, int) else None


def _local_time(value: str, *, full: bool = False) -> str:
    try:
        moment = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return value
    return moment.strftime("%Y-%m-%d %H:%M" if full else "%H:%M:%S")


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

    if not (root / "host-exit-request.json").exists():
        return False
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
