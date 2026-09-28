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


@dataclass(frozen=True)
class TrayState:
    """The complete state the tray needs from the application facade.

    ``update_activity`` holds the progress lines observed for the newest update,
    like the console of a waiting ``vbot update``; ``details`` holds the labeled
    rows of the status window.
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

    def stop(self) -> None: ...


class TrayController:
    """Serialize facade work and project facade state onto one native view."""

    def __init__(self, actions: TrayActions, *, poll_interval: float = 1.0) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._actions = actions
        self._poll_interval = poll_interval
        self._state: TrayState | None = None
        self._status_error = ""
        self._update_requested = False
        self._work: queue.Queue[Callable[[], None] | None] = queue.Queue()
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
        if failed or state.server_state == "conflict":
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
        self._publish()

    def _run_action(self, action: str, callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception as error:  # no facade exception may leave the worker unusable
            if action == "start_update":
                with self._state_lock:
                    self._update_requested = False
            self._record_error(f"Could not {action.replace('_', ' ')}", error)
        else:
            with self._state_lock:
                self._status_error = ""
            if action == "quit" and self._view is not None:
                self._view.stop()

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
    from cli.application.windows_tray import WindowsTray

    controller = TrayController(actions)
    view = WindowsTray(controller, icon_path)
    controller.attach_view(view)
    actions.watch(controller)
    controller.start()
    try:
        view.run()
    finally:
        actions.unwatch()
        controller.close()
