"""Small, injectable Windows notification-area controller.

The controller deliberately knows only the application facade below.  It has no
server-target defaults and no update or installation policy of its own.
"""

from __future__ import annotations

import importlib
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_LOGGER = logging.getLogger("vbot.application.tray")
_SERVER_SHAPES = frozenset({"server", "server-desktop"})
_DESKTOP_SHAPES = frozenset({"server-desktop", "desktop-client"})
_FINISHED_UPDATE_PHASES = frozenset(
    {"completed", "failed", "rolled_back", "needs_attention", "prepared"}
)


@dataclass(frozen=True)
class TrayState:
    """The complete state the tray needs from the application facade."""

    server_state: str
    install_shape: str
    update_phase: str | None = None
    update_message: str = ""
    version: str = ""
    exit_requested: bool = False
    error: str = ""


class TrayActions(Protocol):
    """Application operations exposed to the thin tray adapter."""

    def state(self) -> TrayState: ...

    def start_server(self) -> None: ...

    def stop_server(self) -> None: ...

    def restart_server(self) -> None: ...

    def open_desktop(self) -> None: ...

    def open_browser(self) -> None: ...

    def start_update(self) -> None: ...

    def open_logs(self) -> None: ...

    def quit(self) -> None: ...


@dataclass(frozen=True)
class TrayMenuItem:
    """Backend-independent menu item, used to construct the native menu."""

    label: str
    action: str | None = None
    enabled: bool = True
    default: bool = False


class TrayController:
    """Serialize facade work and expose a testable projection of the tray menu."""

    def __init__(self, actions: TrayActions, *, poll_interval: float = 1.0) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._actions = actions
        self._poll_interval = poll_interval
        self._state: TrayState | None = None
        self._status_error = ""
        self._update_requested = False
        self._work: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._closed = threading.Event()
        self._icon: Any | None = None
        self._state_lock = threading.Lock()
        self._worker = threading.Thread(target=self._run_worker, name="vbot-tray", daemon=True)

    def start(self) -> None:
        """Start the one worker which polls state and executes callbacks."""

        self._worker.start()
        self._work.put(self._poll_state)

    def close(self) -> None:
        """Stop background polling after the native tray loop exits."""

        self._closed.set()
        self._work.put(None)
        self._worker.join(timeout=self._poll_interval + 1.0)

    def attach_icon(self, icon: Any) -> None:
        """Attach the native icon after lazy backend initialization."""

        self._icon = icon
        self._refresh_native_menu()

    def menu_items(self) -> tuple[TrayMenuItem, ...]:
        """Return the currently available menu without importing a GUI library."""

        state = self._current_state()
        if state is None:
            return (TrayMenuItem("vBot: loading…"), TrayMenuItem("Quit vBot", "quit"))

        update_active = self._update_active(state)
        status = self._status_label(state)
        items: list[TrayMenuItem] = [TrayMenuItem(status, enabled=False)]
        if state.install_shape in _DESKTOP_SHAPES:
            items.append(TrayMenuItem("Open Desktop", "open_desktop", default=True))
        if state.install_shape in _SERVER_SHAPES:
            if state.server_state == "running":
                items.extend(
                    (
                        TrayMenuItem("Open in browser", "open_browser"),
                        TrayMenuItem("Restart server", "restart_server", enabled=not update_active),
                        TrayMenuItem("Stop server", "stop_server", enabled=not update_active),
                    )
                )
            elif state.server_state == "stopped":
                items.append(
                    TrayMenuItem("Start server", "start_server", enabled=not update_active)
                )
        items.extend(
            (
                TrayMenuItem("Update", "start_update", enabled=not update_active),
                TrayMenuItem("Open logs", "open_logs"),
                TrayMenuItem("Quit vBot", "quit", enabled=not update_active),
            )
        )
        return tuple(items)

    def invoke(self, action: str) -> None:
        """Queue one enabled facade action; native callbacks return immediately."""

        item = next(
            (candidate for candidate in self.menu_items() if candidate.action == action), None
        )
        if item is None or not item.enabled:
            return
        if action == "start_update":
            with self._state_lock:
                self._update_requested = True
        callback = getattr(self._actions, action)
        self._work.put(lambda: self._run_action(action, callback))
        self._refresh_native_menu()

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
            self._poll_state()

    def _poll_state(self) -> None:
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
        self._refresh_native_menu()

    def _run_action(self, action: str, callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception as error:  # no facade exception may leave the worker unusable
            self._record_error(f"Could not {action.replace('_', ' ')}", error)
        else:
            with self._state_lock:
                self._status_error = ""
            if action == "quit" and self._icon is not None:
                self._icon.stop()

    def _record_error(self, message: str, error: Exception) -> None:
        _LOGGER.exception("%s", message, exc_info=error)
        with self._state_lock:
            self._status_error = f"{message}: {error}"
            if message.endswith("start update"):
                self._update_requested = False
        self._refresh_native_menu()

    def _current_state(self) -> TrayState | None:
        with self._state_lock:
            return self._state

    def _update_active(self, state: TrayState) -> bool:
        with self._state_lock:
            return self._update_requested or (
                state.update_phase is not None and state.update_phase not in _FINISHED_UPDATE_PHASES
            )

    def _status_label(self, state: TrayState) -> str:
        with self._state_lock:
            error = self._status_error
        if error or state.error:
            return error or state.error
        parts = ["vBot"]
        if state.version:
            parts.append(state.version)
        if state.install_shape in _SERVER_SHAPES:
            parts.append(f"server {state.server_state}")
        else:
            parts.append("Desktop Client")
        if state.update_phase is not None:
            detail = state.update_message or state.update_phase.replace("_", " ")
            parts.append(detail)
        return " · ".join(parts)

    def _refresh_native_menu(self) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.menu = _native_menu(self, icon)
            icon.update_menu()
        except Exception:
            _LOGGER.exception("Could not refresh the vBot tray menu")


def run_tray(actions: TrayActions, icon_path: Path) -> None:
    """Run the native tray loop, importing its optional GUI dependencies lazily."""

    pystray = importlib.import_module("pystray")
    from PIL import Image

    controller = TrayController(actions)
    image = Image.open(icon_path)
    icon = pystray.Icon("vbot", image, "vBot")
    controller.attach_icon(icon)
    controller.start()
    try:
        icon.run()
    finally:
        controller.close()


def _native_menu(controller: TrayController, icon: Any) -> Any:
    """Build one pystray menu after its lazy import has happened."""

    pystray = importlib.import_module("pystray")

    def callback(action: str) -> Callable[[Any, Any], None]:
        def invoke(_icon: Any, _item: Any) -> None:
            controller.invoke(action)

        return invoke

    entries = []
    for item in controller.menu_items():
        if item.action is None:
            entries.append(pystray.MenuItem(item.label, None, enabled=False))
        else:
            entries.append(
                pystray.MenuItem(
                    item.label, callback(item.action), enabled=item.enabled, default=item.default
                )
            )
    return pystray.Menu(*entries)
