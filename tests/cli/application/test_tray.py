"""The tray controller remains usable without its optional native backend."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace

from cli.application.tray import TrayActions, TrayController, TrayState


class Actions(TrayActions):
    def __init__(self, state: TrayState) -> None:
        self.current = state
        self.calls: list[str] = []
        self.fail: str | None = None

    def state(self) -> TrayState:
        return self.current

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("test failure")

    def start_server(self) -> None:
        self._call("start_server")

    def stop_server(self) -> None:
        self._call("stop_server")

    def restart_server(self) -> None:
        self._call("restart_server")

    def open_desktop(self) -> None:
        self._call("open_desktop")

    def open_browser(self) -> None:
        self._call("open_browser")

    def start_update(self) -> None:
        self._call("start_update")

    def open_logs(self) -> None:
        self._call("open_logs")

    def open_server_logs(self) -> None:
        self._call("open_server_logs")

    def show_update(self) -> None:
        self._call("show_update")

    def quit(self) -> None:
        self._call("quit")


def _labels(controller: TrayController) -> dict[str, object]:
    return {item.label: item for item in controller.menu_items()}


def _wait_for_call(actions: Actions, name: str) -> None:
    deadline = time.monotonic() + 2
    while name not in actions.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert name in actions.calls


def _wait_for_call_count(actions: Actions, name: str, count: int) -> None:
    deadline = time.monotonic() + 2
    while actions.calls.count(name) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert actions.calls.count(name) == count


def test_server_desktop_menu_projects_only_the_available_actions():
    actions = Actions(TrayState("running", "server-desktop", version="1.2.3"))
    controller = TrayController(actions)
    controller._poll_state()

    menu = _labels(controller)
    assert "Open Desktop" in menu
    assert "Open in browser" in menu
    assert "Restart server" in menu
    assert "Stop server" in menu
    assert "Start server" not in menu
    assert "1.2.3" in next(iter(menu))


def test_desktop_client_never_exposes_or_queues_server_actions():
    actions = Actions(TrayState("running", "desktop-client"))
    controller = TrayController(actions)
    controller._poll_state()

    menu = _labels(controller)
    assert "Open Desktop" in menu
    assert "Open in browser" not in menu
    assert "Start server" not in menu
    assert "Stop server" not in menu
    controller.invoke("start_server")
    assert actions.calls == []


def test_update_is_disabled_while_an_operation_is_active():
    actions = Actions(TrayState("running", "server", update_phase="preparing"))
    controller = TrayController(actions)
    controller._poll_state()

    menu = _labels(controller)
    assert menu["Update"].enabled is False
    controller.invoke("start_update")
    assert actions.calls == []


def test_callbacks_use_one_worker_and_recover_after_a_facade_exception():
    start_entered = threading.Event()
    release_start = threading.Event()
    logs_entered = threading.Event()
    release_logs = threading.Event()
    recovery_polled = threading.Event()
    callback_threads: list[int] = []

    class ControlledActions(Actions):
        def state(self) -> TrayState:
            if logs_entered.is_set() and release_logs.is_set():
                recovery_polled.set()
            return super().state()

        def start_server(self) -> None:
            callback_threads.append(threading.get_ident())
            start_entered.set()
            assert release_start.wait(timeout=5)
            raise RuntimeError("test failure")

        def open_logs(self) -> None:
            callback_threads.append(threading.get_ident())
            logs_entered.set()
            assert release_logs.wait(timeout=5)

    actions = ControlledActions(TrayState("stopped", "server"))
    controller = TrayController(actions, poll_interval=0.01)
    controller._poll_state()
    normal_status = controller.menu_items()[0].label
    controller.start()
    try:
        controller.invoke("start_server")
        assert start_entered.wait(timeout=5)
        controller.invoke("open_logs")
        assert not logs_entered.is_set()

        release_start.set()
        assert logs_entered.wait(timeout=5)
        # Entering the next callback proves the first exception was handled.
        # Keep it blocked so successful recovery cannot clear the status yet.
        assert controller.menu_items()[0].label != normal_status
        assert callback_threads[0] == callback_threads[1] != threading.get_ident()

        release_logs.set()
        assert recovery_polled.wait(timeout=5)
        assert controller.menu_items()[0].label == normal_status
    finally:
        release_start.set()
        release_logs.set()
        controller.close()


def test_update_request_disables_duplicate_clicks_until_facade_reports_completion():
    actions = Actions(TrayState("running", "server"))
    controller = TrayController(actions, poll_interval=0.01)
    controller._poll_state()
    controller.start()
    try:
        controller.invoke("start_update")
        _wait_for_call(actions, "start_update")
        assert _labels(controller)["Update"].enabled is False

        actions.current = replace(actions.current, update_phase="completed")
        controller._poll_state()
        assert _labels(controller)["Update"].enabled is True
        controller.invoke("start_update")
        _wait_for_call_count(actions, "start_update", 2)
    finally:
        controller.close()


def test_terminal_update_status_keeps_server_health_visible_and_allows_next_update():
    actions = Actions(
        TrayState("stopped", "server", update_phase="completed", update_message="Updated")
    )
    controller = TrayController(actions)
    controller._poll_state()

    menu = _labels(controller)
    assert "Server stopped" in next(iter(menu))
    assert "Updated" not in next(iter(menu))
    assert menu["Update"].enabled is True


def test_facade_startup_error_is_visible_without_removing_recovery_actions():
    actions = Actions(TrayState("stopped", "server", error="Startup failed: port is occupied"))
    controller = TrayController(actions)
    controller._poll_state()

    menu = _labels(controller)
    actions.current = replace(actions.current, error="")
    controller._poll_state()
    assert next(iter(menu)) != controller.menu_items()[0].label
    assert menu["Start server"].enabled is True
    assert menu["Update"].enabled is True
    assert menu["Application logs"].enabled is True


def test_unchanged_poll_does_not_replace_the_native_menu(monkeypatch):
    class Icon:
        menu = None
        updates = 0

        def update_menu(self) -> None:
            self.updates += 1

    actions = Actions(TrayState("running", "server", version="0.4.2"))
    controller = TrayController(actions)
    icon = Icon()
    monkeypatch.setattr("cli.application.tray._native_menu", lambda *_args, **_kwargs: object())
    controller.attach_icon(icon)
    initial_updates = icon.updates

    controller._poll_state()
    controller._poll_state()

    assert icon.updates == initial_updates + 1


def test_windows_menu_metrics_scale_for_per_monitor_dpi():
    if os.name != "nt":
        return
    from cli.application.windows_tray import _scale

    assert _scale(28, 96) == 28
    assert _scale(28, 144) == 42
    assert _scale(28, 192) == 56


def test_windows_owner_draw_paints_explicit_dark_hover_background(monkeypatch):
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    from cli.application import windows_tray

    windows_tray._gdi32.CreateCompatibleDC.restype = wintypes.HDC
    windows_tray._gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    windows_tray._gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    windows_tray._gdi32.CreateCompatibleBitmap.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
    ]
    windows_tray._gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    windows_tray._gdi32.DeleteDC.argtypes = [wintypes.HDC]

    icon = object.__new__(windows_tray.WindowsTrayIcon)
    icon._running = False
    icon._icon_handle = None
    icon._labels = {1: "Open Desktop"}
    icon._menu_hwnd = None
    icon._hwnd = None
    icon._dpi = lambda: 96
    monkeypatch.setattr(windows_tray, "_apps_use_dark_theme", lambda: True)
    screen = windows_tray._user32.GetDC(None)
    dc = windows_tray._gdi32.CreateCompatibleDC(screen)
    bitmap = windows_tray._gdi32.CreateCompatibleBitmap(screen, 240, 32)
    previous = windows_tray._gdi32.SelectObject(dc, bitmap)
    draw = windows_tray._DrawItem(
        itemID=1,
        itemState=windows_tray._ODS_SELECTED,
        hDC=dc,
        rcItem=wintypes.RECT(0, 0, 240, 32),
    )
    try:
        assert icon._on_draw_item(0, ctypes.addressof(draw)) == 1
        assert windows_tray._gdi32.GetPixel(dc, 12, 12) == 0x00383838
        assert windows_tray._gdi32.GetPixel(dc, 2, 2) == 0x00202020
    finally:
        windows_tray._gdi32.SelectObject(dc, previous)
        windows_tray._gdi32.DeleteObject(bitmap)
        windows_tray._gdi32.DeleteDC(dc)
        windows_tray._user32.ReleaseDC(None, screen)


def test_windows_right_click_tracks_exactly_one_popup_and_one_callback(monkeypatch):
    if os.name != "nt":
        return
    from cli.application import windows_tray

    calls: list[str] = []
    icon = object.__new__(windows_tray.WindowsTrayIcon)
    icon._running = False
    icon._icon_handle = None
    icon._menu_handle = (17, [lambda _icon: calls.append("callback")])
    icon._hwnd = 23
    icon._menu_open = False
    icon._menu_pending = False
    monkeypatch.setattr(windows_tray._win32.win32, "SetForegroundWindow", lambda _hwnd: None)
    monkeypatch.setattr(windows_tray._win32.win32, "GetCursorPos", lambda _point: True)
    monkeypatch.setattr(
        windows_tray._win32.win32,
        "TrackPopupMenuEx",
        lambda *_args: calls.append("popup") or 1,
    )
    monkeypatch.setattr(
        windows_tray._win32.Icon,
        "_on_notify",
        lambda *_args: calls.append("base"),
    )

    assert icon._on_notify(0, windows_tray._win32.win32.WM_RBUTTONUP) is None
    assert calls == ["popup", "callback"]


def test_windows_menu_update_is_deferred_even_on_ui_thread_while_popup_is_open():
    if os.name != "nt":
        return
    from cli.application import windows_tray

    applied: list[bool] = []
    icon = object.__new__(windows_tray.WindowsTrayIcon)
    icon._running = False
    icon._icon_handle = None
    icon._hwnd = 23
    icon._thread = threading.current_thread()
    icon._menu_open = True
    icon._menu_pending = False
    icon._apply_menu = lambda: applied.append(True)

    icon._update_menu()

    assert icon._menu_pending is True
    assert applied == []


def test_exit_request_queues_quit_and_stops_the_icon_only_after_success():
    class Icon:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

        def update_menu(self) -> None:
            pass

    actions = Actions(TrayState("stopped", "server", exit_requested=True))
    controller = TrayController(actions, poll_interval=0.01)
    icon = Icon()
    controller.attach_icon(icon)
    controller.start()
    try:
        _wait_for_call(actions, "quit")
        deadline = time.monotonic() + 2
        while not icon.stopped and time.monotonic() < deadline:
            time.sleep(0.01)
        assert icon.stopped is True
    finally:
        controller.close()


def test_busy_external_exit_request_failure_keeps_the_tray_visible():
    class Icon:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

        def update_menu(self) -> None:
            pass

    actions = Actions(TrayState("running", "server", update_phase="preparing", exit_requested=True))
    actions.fail = "quit"
    controller = TrayController(actions, poll_interval=0.05)
    icon = Icon()
    controller.attach_icon(icon)
    controller.start()
    try:
        _wait_for_call(actions, "quit")
        assert icon.stopped is False
        assert _labels(controller)["Quit vBot"].enabled is False
    finally:
        controller.close()


def test_quit_stops_the_icon_only_after_the_facade_quits_successfully():
    class Icon:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    actions = Actions(TrayState("stopped", "server"))
    controller = TrayController(actions, poll_interval=0.01)
    icon = Icon()
    controller.attach_icon(icon)
    controller._poll_state()
    controller.start()
    try:
        controller.invoke("quit")
        assert icon.stopped is False
        _wait_for_call(actions, "quit")
        deadline = time.monotonic() + 2
        while not icon.stopped and time.monotonic() < deadline:
            time.sleep(0.01)
        assert icon.stopped is True

        actions.fail = "quit"
        icon.stopped = False
        controller.invoke("quit")
        _wait_for_call_count(actions, "quit", 2)
        time.sleep(0.05)
        assert icon.stopped is False
    finally:
        controller.close()
