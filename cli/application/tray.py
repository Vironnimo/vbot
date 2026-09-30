"""Platform-neutral notification-area controller for the packaged host.

The controller deliberately knows only the application facade below and a
native view. It has no server-target defaults and no update or installation
policy of its own.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cli.application.notifications import Toast

_LOGGER = logging.getLogger("vbot.application.tray")
_SERVER_SHAPES = frozenset({"server", "server-desktop"})
_DESKTOP_SHAPES = frozenset({"server-desktop", "desktop-client"})
_FINISHED_UPDATE_PHASES = frozenset(
    {"completed", "failed", "rolled_back", "needs_attention", "prepared"}
)
_FAILED_UPDATE_PHASES = frozenset({"failed", "rolled_back", "needs_attention"})
#: Earliest retry after a failed restart into the active version.
_RESTART_RETRY_SECONDS = 600.0


@dataclass(frozen=True)
class TrayState:
    """The complete state the tray needs from the application facade.

    ``update_activity`` holds the progress lines observed for the newest update,
    like the console of a waiting ``vbot update``; ``details`` holds the labeled
    rows of the status window. ``restart_pending`` means another version became
    active since this tray started and no update is running, so the tray should
    hand over to a successor running the active version.
    """

    server_state: str
    install_shape: str
    update_phase: str | None = None
    update_message: str = ""
    version: str = ""
    exit_requested: bool = False
    error: str = ""
    server_url: str = ""
    update_activity: tuple[str, ...] = ()
    details: tuple[tuple[str, str], ...] = ()
    restart_pending: bool = False


class TraySink(Protocol):
    """Receives facade observations from any thread."""

    def changed(self) -> None: ...

    def show(self, toast: Toast) -> None: ...

    def dismiss(self, key: str) -> None: ...


class TrayActions(Protocol):
    """Application operations exposed to the thin tray adapter."""

    def state(self) -> TrayState: ...

    def watch(self, sink: TraySink) -> None: ...

    def unwatch(self) -> None: ...

    def start_server(self) -> None: ...

    def stop_server(self) -> None: ...

    def restart_server(self) -> None: ...

    def open_desktop(self) -> None: ...

    def open_browser(self) -> None: ...

    def open_session(self, agent: str, session: str) -> None: ...

    def start_update(self) -> None: ...

    def open_logs(self) -> None: ...

    def open_server_logs(self) -> None: ...

    def restart(self) -> None: ...

    def quit(self) -> None: ...


@dataclass(frozen=True)
class TrayMenuItem:
    """Backend-independent menu item, used to construct the native menu."""

    label: str
    action: str | None = None
    enabled: bool = True
    default: bool = False
    separator: bool = False


@dataclass(frozen=True)
class TrayStatus:
    """Content of the status window."""

    title: str
    headline: str
    failed: bool
    rows: tuple[tuple[str, str], ...]
    activity: tuple[str, ...]
    buttons: tuple[TrayMenuItem, ...]


@dataclass(frozen=True)
class TrayPresentation:
    """Everything a native view shows; views apply only what changed.

    ``icon`` is ``normal``, ``stopped``, ``updating`` or ``error``.
    """

    icon: str
    tooltip: str
    menu: tuple[TrayMenuItem, ...]
    status: TrayStatus


class TrayView(Protocol):
    """Native tray surface; every method may be called from any thread."""

    def present(self, presentation: TrayPresentation) -> None: ...

    def show_toast(self, toast: Toast) -> None: ...

    def dismiss_toast(self, key: str) -> None: ...

    def show_status(self) -> None: ...

    def interacting(self) -> bool:
        """Whether the user may be using the tray: open menu, shown toast or status window."""
        ...

    def stop(self) -> None: ...


class TrayController:
    """Serialize facade work and project facade state onto one native view."""

    def __init__(
        self,
        actions: TrayActions,
        *,
        poll_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._actions = actions
        self._poll_interval = poll_interval
        self._clock = clock
        self._state: TrayState | None = None
        self._status_error = ""
        self._update_requested = False
        self._restarted = False
        self._restart_retry_at: float | None = None
        self._work: queue.Queue[Callable[[], object] | None] = queue.Queue()
        self._poll_queued = threading.Event()
        self._closed = threading.Event()
        self._view: TrayView | None = None
        self._state_lock = threading.Lock()
        self._published: TrayPresentation | None = None
        self._worker = threading.Thread(target=self._run_worker, name="vbot-tray", daemon=True)

    def start(self) -> None:
        """Start the one worker which refreshes state and executes callbacks."""

        self._worker.start()
        self.changed()

    def close(self) -> None:
        """Stop the worker after the native tray loop exits."""

        self._closed.set()
        self._work.put(None)
        self._worker.join(timeout=self._poll_interval + 1.0)

    def attach_view(self, view: TrayView) -> None:
        """Attach the native view after lazy backend initialization."""

        self._view = view
        self._publish(force=True)

    def menu_items(self) -> tuple[TrayMenuItem, ...]:
        """Return the currently available menu without importing a GUI library."""

        state = self._current_state()
        if state is None:
            return (TrayMenuItem("vBot\nStarting…"), TrayMenuItem("Quit vBot", "quit"))

        update_active = self._update_active(state)
        title, status, _failed = self._status_line(state)
        separator = TrayMenuItem("", enabled=False, separator=True)
        items: list[TrayMenuItem] = [TrayMenuItem(f"{title}\n{status}", enabled=False), separator]
        if state.install_shape in _DESKTOP_SHAPES:
            items.append(TrayMenuItem("Open Desktop", "open_desktop", default=True))
        if state.install_shape in _SERVER_SHAPES:
            if state.server_state == "running":
                items.extend(
                    (
                        TrayMenuItem(
                            "Open in browser",
                            "open_browser",
                            default=state.install_shape not in _DESKTOP_SHAPES,
                        ),
                        separator,
                        TrayMenuItem("Restart server", "restart_server", enabled=not update_active),
                        TrayMenuItem("Stop server", "stop_server", enabled=not update_active),
                    )
                )
            elif state.server_state == "unresponsive":
                # The server process lives, so a start would be refused; only its
                # own restart or stop can recover it.
                items.extend(
                    (
                        separator,
                        TrayMenuItem("Restart server", "restart_server", enabled=not update_active),
                        TrayMenuItem("Stop server", "stop_server", enabled=not update_active),
                    )
                )
            elif state.server_state in {"stopped", "conflict"}:
                items.extend(
                    (
                        separator,
                        TrayMenuItem("Start server", "start_server", enabled=not update_active),
                    )
                )
        items.extend(
            (
                separator,
                TrayMenuItem("Update", "start_update", enabled=not update_active),
                TrayMenuItem("Status…", "show_status"),
                TrayMenuItem("Application logs", "open_logs"),
            )
        )
        if state.install_shape in _SERVER_SHAPES:
            items.append(TrayMenuItem("Server logs", "open_server_logs"))
        items.extend((separator, TrayMenuItem("Quit vBot", "quit", enabled=not update_active)))
        return tuple(items)

    def presentation(self) -> TrayPresentation:
        """Return the complete projection a native view displays."""

        menu = self.menu_items()
        state = self._current_state()
        if state is None:
            status = TrayStatus("vBot", "Starting…", False, (), (), ())
            return TrayPresentation("normal", "vBot", menu, status)
        title, line, failed = self._status_line(state)
        update_active = self._update_active(state)
        if failed or state.server_state in {"conflict", "unresponsive"}:
            icon = "error"
        elif update_active:
            icon = "updating"
        elif state.install_shape in _SERVER_SHAPES and state.server_state == "stopped":
            icon = "stopped"
        else:
            icon = "normal"
        with self._state_lock:
            error = self._status_error or state.error
        buttons = tuple(
            item
            for item in menu
            if item.action in {"start_server", "restart_server", "start_update", "open_logs"}
        )
        rows = state.details
        if error:
            rows = (("Last error", error), *rows)
        status = TrayStatus(title, line, failed, rows, state.update_activity, buttons)
        return TrayPresentation(icon, f"{title} — {line}"[:127], menu, status)

    def invoke(self, action: str) -> None:
        """Queue one enabled facade action; native callbacks return immediately."""

        item = next(
            (candidate for candidate in self.menu_items() if candidate.action == action), None
        )
        if item is None or not item.enabled:
            return
        if action == "show_status":
            if self._view is not None:
                self._view.show_status()
            return
        if action == "start_update":
            with self._state_lock:
                self._update_requested = True
        callback = getattr(self._actions, action)
        self._work.put(lambda: self._run_action(action, callback))
        self._publish()

    def invoke_default(self) -> None:
        """Run the primary action of a left click on the notification-area icon."""

        default = next((item for item in self.menu_items() if item.default and item.enabled), None)
        self.invoke(default.action if default is not None and default.action else "show_status")

    def activate_toast(self, toast: Toast) -> None:
        """Open what a clicked toast refers to."""

        if toast.session is None:
            self.invoke("show_status")
            return
        agent, session = toast.session
        self._work.put(
            lambda: self._run_action(
                "open_session", lambda: self._actions.open_session(agent, session)
            )
        )

    def changed(self) -> None:
        """Refresh facade state soon; repeated signals coalesce."""

        if not self._poll_queued.is_set():
            self._poll_queued.set()
            self._work.put(self._poll_state)

    def show(self, toast: Toast) -> None:
        if self._view is not None:
            self._view.show_toast(toast)

    def dismiss(self, key: str) -> None:
        if self._view is not None:
            self._view.dismiss_toast(key)

    def _run_worker(self) -> None:
        while not self._closed.is_set():
            try:
                work = self._work.get(timeout=self._poll_interval)
            except queue.Empty:
                self._poll_state()
                continue
            if work is None:
                return
            work()
            if work != self._poll_state:
                self._poll_state()

    def _poll_state(self) -> None:
        self._poll_queued.clear()
        try:
            state = self._actions.state()
            if not isinstance(state, TrayState):
                raise TypeError("TrayActions.state() must return TrayState")
        except Exception as error:  # facade failures must never kill the tray worker
            self._record_error("Could not refresh vBot status", error)
            return
        with self._state_lock:
            self._state = state
            if state.update_phase in _FINISHED_UPDATE_PHASES:
                self._update_requested = False
        if state.exit_requested:
            self._run_action("quit", self._actions.quit)
        elif state.restart_pending:
            self._restart(state)
        self._publish()

    def _restart(self, state: TrayState) -> None:
        """Hand the tray over to the active version once nobody is using it.

        Runs on the worker, so attempts never overlap. A successful handoff stops
        the view without quitting: the server keeps running for the successor.
        """

        view = self._view
        if (
            view is None
            or self._restarted
            or self._update_active(state)
            or (self._restart_retry_at is not None and self._clock() < self._restart_retry_at)
            # A queued tray action goes first; the next poll checks again.
            or not self._work.empty()
            or view.interacting()
        ):
            return
        if self._run_action("restart_tray", self._actions.restart):
            self._restarted = True
        else:
            self._restart_retry_at = self._clock() + _RESTART_RETRY_SECONDS

    def _run_action(self, action: str, callback: Callable[[], None]) -> bool:
        try:
            callback()
        except Exception as error:  # no facade exception may leave the worker unusable
            if action == "start_update":
                with self._state_lock:
                    self._update_requested = False
            self._record_error(f"Could not {action.replace('_', ' ')}", error)
            return False
        with self._state_lock:
            self._status_error = ""
        if action in {"quit", "restart_tray"} and self._view is not None:
            self._view.stop()
        return True

    def _record_error(self, message: str, error: Exception) -> None:
        _LOGGER.exception("%s", message, exc_info=error)
        with self._state_lock:
            self._status_error = f"{message}: {error}"
        self._publish()

    def _current_state(self) -> TrayState | None:
        with self._state_lock:
            return self._state

    def _update_active(self, state: TrayState) -> bool:
        with self._state_lock:
            return self._update_requested or (
                state.update_phase is not None and state.update_phase not in _FINISHED_UPDATE_PHASES
            )

    def _status_line(self, state: TrayState) -> tuple[str, str, bool]:
        """Return the title, the one-line status and whether it reports a failure."""

        with self._state_lock:
            error = self._status_error
        title = f"vBot {state.version}" if state.version else "vBot"
        if error or state.error:
            return title, "Action failed · see Status", True
        if state.update_phase in _FAILED_UPDATE_PHASES:
            return title, "Update failed · see Status", True
        if self._update_active(state):
            return title, "Updating…", False
        if state.update_phase == "prepared":
            return title, "Update prepared · not active", False
        if state.install_shape in _SERVER_SHAPES:
            status = {
                "running": "Server running",
                "unresponsive": "Server not responding",
                "stopped": "Server stopped",
                "conflict": "Server port is occupied",
                "unknown": "Checking server…",
            }.get(state.server_state, "Server status unavailable")
        else:
            status = "Desktop Client"
        return title, status, False

    def _publish(self, *, force: bool = False) -> None:
        view = self._view
        if view is None:
            return
        presentation = self.presentation()
        with self._state_lock:
            if not force and presentation == self._published:
                return
            self._published = presentation
        try:
            view.present(presentation)
        except Exception:
            _LOGGER.exception("Could not refresh the vBot tray")


def run_tray(actions: TrayActions, icon_path: Path) -> None:
    """Run the native tray loop, importing its platform backend lazily."""

    if sys.platform != "win32":
        raise RuntimeError("The vBot tray host requires Windows")
    from cli.application import windows_tray

    controller = TrayController(actions)
    view = windows_tray.WindowsTray(controller, icon_path)
    controller.attach_view(view)
    actions.watch(controller)
    controller.start()
    try:
        view.run()
    finally:
        actions.unwatch()
        controller.close()
