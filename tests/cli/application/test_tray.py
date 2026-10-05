"""The tray controller and its native Windows view keep their user-facing contracts."""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from typing import override

import pytest

from cli.application.notifications import Toast
from cli.application.tray import (
    TrayActions,
    TrayController,
    TrayPresentation,
    TraySink,
    TrayState,
)


class Actions(TrayActions):
    def __init__(self, state: TrayState) -> None:
        self.current = state
        self.calls: list[str] = []
        self.fail: str | None = None

    @override
    def state(self) -> TrayState:
        return self.current

    @override
    def watch(self, sink: TraySink) -> None:
        self.calls.append("watch")

    @override
    def unwatch(self) -> None:
        self.calls.append("unwatch")

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("test failure")

    @override
    def start_server(self) -> None:
        self._call("start_server")

    @override
    def stop_server(self) -> None:
        self._call("stop_server")

    @override
    def restart_server(self) -> None:
        self._call("restart_server")

    @override
    def open_desktop(self) -> None:
        self._call("open_desktop")

    @override
    def open_browser(self) -> None:
        self._call("open_browser")

    @override
    def open_session(self, agent: str, session: str) -> None:
        self._call(f"open_session:{agent}:{session}")

    @override
    def start_update(self) -> None:
        self._call("start_update")

    @override
    def open_logs(self) -> None:
        self._call("open_logs")

    @override
    def open_server_logs(self) -> None:
        self._call("open_server_logs")

    @override
    def restart(self) -> None:
        self._call("restart")

    @override
    def quit(self) -> None:
        self._call("quit")


class View:
    def __init__(self) -> None:
        self.presented: list[TrayPresentation] = []
        self.toasts: list[Toast] = []
        self.dismissed: list[str] = []
        self.status_shown = 0
        self.stopped = False
        self.busy = False

    def present(self, presentation: TrayPresentation) -> None:
        self.presented.append(presentation)

    def show_toast(self, toast: Toast) -> None:
        self.toasts.append(toast)

    def dismiss_toast(self, key: str) -> None:
        self.dismissed.append(key)

    def show_status(self) -> None:
        self.status_shown += 1

    def interacting(self) -> bool:
        return self.busy

    def stop(self) -> None:
        self.stopped = True


def _labels(controller: TrayController) -> dict[str, object]:
    return {item.label: item for item in controller.menu_items()}


def _wait_until(condition, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition()


@pytest.mark.parametrize(
    ("server_state", "offered", "withheld"),
    [
        pytest.param(
            "running",
            {"Open in browser", "Restart server", "Stop server"},
            {"Start server"},
            id="running",
        ),
        pytest.param(
            "unresponsive",
            {"Restart server", "Stop server"},
            {"Open in browser", "Start server"},
            id="not-responding",
        ),
        # Started elsewhere: a start that never finishes can still be stopped.
        pytest.param(
            "starting",
            {"Stop server"},
            {"Open in browser", "Start server", "Restart server"},
            id="starting",
        ),
        pytest.param(
            "stopping",
            set(),
            {"Open in browser", "Start server", "Restart server", "Stop server"},
            id="stopping",
        ),
    ],
)
def test_server_desktop_menu_projects_only_the_available_actions(
    server_state: str, offered: set[str], withheld: set[str]
):
    actions = Actions(TrayState(server_state, "server-desktop", version="1.2.3"))
    controller = TrayController(actions)
    controller._poll_state()

    menu = _labels(controller)
    assert {"Open Desktop", "Status…", *offered} <= menu.keys()
    assert not withheld & menu.keys()
    assert "1.2.3" in next(iter(menu))


def test_desktop_client_never_exposes_or_queues_server_actions():
    actions = Actions(TrayState("not_applicable", "desktop-client"))
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


@pytest.mark.parametrize(
    ("state", "icon", "status"),
    [
        pytest.param(TrayState("running", "server"), "normal", "Server running", id="running"),
        pytest.param(TrayState("stopped", "server"), "stopped", "Server stopped", id="stopped"),
        pytest.param(
            TrayState("unresponsive", "server"),
            "error",
            "Server not responding",
            id="not-responding",
        ),
        pytest.param(TrayState("starting", "server"), "busy", "Server starting…", id="starting"),
        pytest.param(TrayState("stopping", "server"), "busy", "Server stopping…", id="stopping"),
        pytest.param(
            TrayState("running", "server", update_phase="verifying"),
            "busy",
            "Updating…",
            id="updating",
        ),
        pytest.param(
            TrayState("running", "server", update_phase="rolled_back"),
            "error",
            "update failed",
            id="failed-update",
        ),
        pytest.param(
            TrayState("conflict", "server"), "error", "Server port is occupied", id="conflict"
        ),
        pytest.param(
            TrayState("not_applicable", "desktop-client"),
            "normal",
            "Desktop Client",
            id="client",
        ),
    ],
)
def test_icon_and_tooltip_show_the_application_state(state: TrayState, icon: str, status: str):
    controller = TrayController(Actions(replace(state, version="0.5.0")))
    controller._poll_state()

    presentation = controller.presentation()
    assert presentation.icon == icon
    assert presentation.tooltip.startswith("vBot 0.5.0")
    assert status in presentation.tooltip
    assert status in presentation.status.headline


def test_status_window_lists_details_activity_and_recovery_actions():
    actions = Actions(
        TrayState(
            "stopped",
            "server",
            version="0.5.0",
            activity=("12:00:00  Server stopped from the tray",),
            details=(("Server", "http://127.0.0.1:8420"),),
            error="Startup failed: port is occupied",
        )
    )
    controller = TrayController(actions)
    controller._poll_state()

    status = controller.presentation().status
    assert status.title == "vBot 0.5.0"
    assert status.failed is True
    # A failed action never hides how the server stands.
    assert "Server stopped" in status.headline
    assert status.rows[0] == ("Last error", "Startup failed: port is occupied")
    assert ("Server", "http://127.0.0.1:8420") in status.rows
    assert status.activity == ("12:00:00  Server stopped from the tray",)
    assert [item.action for item in status.buttons] == [
        "start_server",
        "start_update",
        "open_logs",
    ]

    actions.current = TrayState(
        "running", "server", running_since="2026-10-05T12:58:29.000000Z", activity=()
    )
    controller._poll_state()
    assert controller.presentation().status.headline.startswith("Server running since ")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(TrayState("running", "server-desktop"), "open_desktop", id="desktop"),
        pytest.param(TrayState("running", "server"), "open_browser", id="browser"),
        pytest.param(TrayState("stopped", "server"), "status", id="status"),
    ],
)
def test_left_click_runs_the_primary_action(state: TrayState, expected: str):
    actions = Actions(state)
    view = View()
    controller = TrayController(actions, poll_interval=0.01)
    controller.attach_view(view)
    controller._poll_state()
    controller.start()
    try:
        controller.invoke_default()
        if expected == "status":
            assert view.status_shown == 1
        else:
            _wait_until(lambda: expected in actions.calls)
    finally:
        controller.close()


def test_clicked_toast_opens_its_session_or_the_status_window():
    actions = Actions(TrayState("running", "server-desktop"))
    view = View()
    controller = TrayController(actions, poll_interval=0.01)
    controller.attach_view(view)
    controller._poll_state()
    controller.start()
    try:
        controller.activate_toast(
            Toast("run:r1", "run_completed", "Coder finished", "Plan", session=("coder", "s1"))
        )
        _wait_until(lambda: "open_session:coder:s1" in actions.calls)
        controller.activate_toast(Toast("server", "server_stopped", "vBot server stopped", ""))
        assert view.status_shown == 1
    finally:
        controller.close()


def test_callbacks_use_one_worker_and_recover_after_a_facade_exception():
    start_entered = threading.Event()
    release_start = threading.Event()
    logs_entered = threading.Event()
    release_logs = threading.Event()
    callback_threads: list[int] = []

    class ControlledActions(Actions):
        @override
        def start_server(self) -> None:
            callback_threads.append(threading.get_ident())
            start_entered.set()
            assert release_start.wait(timeout=5)
            raise RuntimeError("test failure")

        @override
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
        # A state poll can precede the action worker clearing its error.
        _wait_until(lambda: controller.menu_items()[0].label == normal_status)
    finally:
        release_start.set()
        release_logs.set()
        controller.close()


def test_a_running_lifecycle_action_shows_its_transition_while_the_state_keeps_refreshing():
    start_entered = threading.Event()
    release_start = threading.Event()

    class SlowStart(Actions):
        @override
        def start_server(self) -> None:
            super().start_server()
            start_entered.set()
            assert release_start.wait(timeout=5)
            self.current = replace(self.current, server_state="running")

    actions = SlowStart(TrayState("stopped", "server", version="0.5.0"))
    view = View()
    controller = TrayController(actions, poll_interval=0.01)
    controller.attach_view(view)
    # The tray's initial server start runs like a clicked one, once the icon shows.
    controller.start(start_server=True)
    try:
        assert start_entered.wait(timeout=5)
        _wait_until(lambda: "Server starting…" in view.presented[-1].tooltip)
        assert view.presented[-1].icon == "busy"
        assert not {"Start server", "Stop server", "Restart server"} & _labels(controller).keys()
        # The facade is still polled while the action runs.
        actions.current = replace(actions.current, version="0.5.1")
        _wait_until(lambda: view.presented[-1].tooltip.startswith("vBot 0.5.1"))

        release_start.set()
        _wait_until(lambda: "Server running" in view.presented[-1].tooltip)
        assert "Stop server" in _labels(controller)
    finally:
        release_start.set()
        controller.close()
    assert actions.calls == ["start_server"]


def test_update_request_disables_duplicate_clicks_until_facade_reports_completion():
    actions = Actions(TrayState("running", "server"))
    controller = TrayController(actions, poll_interval=0.01)
    controller._poll_state()
    controller.start()
    try:
        controller.invoke("start_update")
        _wait_until(lambda: "start_update" in actions.calls)
        assert _labels(controller)["Update"].enabled is False

        actions.current = replace(actions.current, update_phase="completed")
        controller._poll_state()
        assert _labels(controller)["Update"].enabled is True
        controller.invoke("start_update")
        _wait_until(lambda: actions.calls.count("start_update") == 2)
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


def test_unchanged_poll_does_not_present_again():
    actions = Actions(TrayState("running", "server", version="0.4.2"))
    controller = TrayController(actions)
    view = View()
    controller.attach_view(view)
    initial = len(view.presented)

    controller._poll_state()
    controller._poll_state()

    assert len(view.presented) == initial + 1


def test_exit_request_queues_quit_and_stops_the_view_only_after_success():
    actions = Actions(TrayState("stopped", "server", exit_requested=True))
    controller = TrayController(actions, poll_interval=0.01)
    view = View()
    controller.attach_view(view)
    controller.start()
    try:
        _wait_until(lambda: "quit" in actions.calls)
        _wait_until(lambda: view.stopped)
    finally:
        controller.close()


def test_busy_external_exit_request_failure_keeps_the_tray_visible():
    actions = Actions(TrayState("running", "server", update_phase="preparing", exit_requested=True))
    actions.fail = "quit"
    controller = TrayController(actions, poll_interval=0.05)
    view = View()
    controller.attach_view(view)
    controller.start()
    try:
        _wait_until(lambda: "quit" in actions.calls)
        assert view.stopped is False
        assert _labels(controller)["Quit vBot"].enabled is False
    finally:
        controller.close()


def test_quit_stops_the_view_only_after_the_facade_quits_successfully():
    actions = Actions(TrayState("stopped", "server"))
    controller = TrayController(actions, poll_interval=0.01)
    view = View()
    controller.attach_view(view)
    controller._poll_state()
    controller.start()
    try:
        controller.invoke("quit")
        _wait_until(lambda: view.stopped)

        actions.fail = "quit"
        view.stopped = False
        controller.invoke("quit")
        _wait_until(lambda: actions.calls.count("quit") == 2)
        time.sleep(0.05)
        assert view.stopped is False
    finally:
        controller.close()


def _run_queued(controller: TrayController) -> None:
    """Run the queued actions on the test's thread, as the action thread would."""

    while not controller._work.empty():
        work = controller._work.get_nowait()
        assert work is not None
        work()


def test_pending_restart_waits_for_an_idle_tray_and_retries_ten_minutes_after_a_failure():
    now = [1000.0]
    actions = Actions(TrayState("running", "server", restart_pending=True))
    view = View()
    controller = TrayController(actions, clock=lambda: now[0])
    controller.attach_view(view)

    view.busy = True
    controller._poll_state()
    _run_queued(controller)
    assert actions.calls == []

    view.busy = False
    actions.fail = "restart"
    controller._poll_state()
    _run_queued(controller)
    assert actions.calls == ["restart"]
    assert view.stopped is False
    assert controller.presentation().status.rows[0][0] == "Last error"
    now[0] += 599
    controller._poll_state()
    _run_queued(controller)
    assert actions.calls == ["restart"]

    now[0] += 1
    actions.fail = None
    # A queued tray action goes first; the handoff never quits or stops the server.
    controller.invoke("open_logs")
    controller._poll_state()
    _run_queued(controller)
    assert actions.calls == ["restart", "open_logs"]
    controller._poll_state()
    _run_queued(controller)
    assert view.stopped is True
    controller._poll_state()
    _run_queued(controller)
    assert actions.calls == ["restart", "open_logs", "restart"]


class Commands:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def invoke(self, action: str) -> None:
        self.calls.append(action)

    def invoke_default(self) -> None:
        self.calls.append("default")

    def activate_toast(self, toast: Toast) -> None:
        self.calls.append(toast)


def _tray(commands: Commands, monkeypatch: pytest.MonkeyPatch, tmp_path, **options):
    if sys.platform != "win32":
        pytest.skip("native Windows tray")
    from cli.application import windows_tray

    tray = windows_tray.WindowsTray(commands, tmp_path / "icon.ico", **options)
    tray._hwnd = 23
    notified: list[tuple[int, str]] = []

    def notify(message, data):
        notified.append((message, data._obj.szInfo))
        return True

    monkeypatch.setattr(windows_tray.native.shell32, "Shell_NotifyIconW", notify)
    monkeypatch.setattr(windows_tray.WindowsTray, "_icon", lambda *_args: 0)
    return tray, notified


def test_windows_menu_metrics_scale_for_per_monitor_dpi():
    if sys.platform != "win32":
        pytest.skip("native Windows tray")
    from cli.application.windows_native import scale

    assert scale(28, 96) == 28
    assert scale(28, 144) == 42
    assert scale(28, 192) == 56


def test_windows_owner_draw_paints_explicit_dark_hover_background():
    if sys.platform != "win32":
        pytest.skip("native Windows tray")
    from ctypes import wintypes

    from cli.application import windows_native as native
    from cli.application.tray import TrayMenuItem
    from cli.application.windows_tray import paint_menu_item

    screen = native.user32.GetDC(None)
    dc = native.gdi32.CreateCompatibleDC(screen)
    bitmap = native.gdi32.CreateCompatibleBitmap(screen, 240, 32)
    previous = native.gdi32.SelectObject(dc, bitmap)
    draw = native.DRAWITEMSTRUCT(itemState=0x1, hDC=dc, rcItem=wintypes.RECT(0, 0, 240, 32))
    try:
        paint_menu_item(draw, TrayMenuItem("Open Desktop", "open_desktop"), 96, native.DARK)
        assert native.gdi32.GetPixel(dc, 12, 12) == 0x00383838
        assert native.gdi32.GetPixel(dc, 2, 2) == 0x00202020
    finally:
        native.gdi32.SelectObject(dc, previous)
        native.gdi32.DeleteObject(bitmap)
        native.gdi32.DeleteDC(dc)
        native.user32.ReleaseDC(None, screen)


def test_windows_right_click_sets_an_arrow_cursor_and_runs_one_chosen_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    if sys.platform != "win32":
        pytest.skip("native Windows tray")
    from cli.application import windows_tray
    from cli.application.tray import TrayMenuItem, TrayStatus

    commands = Commands()
    tray, _notified = _tray(commands, monkeypatch, tmp_path)
    menu = (TrayMenuItem("vBot\nServer running"), TrayMenuItem("Status…", "show_status"))
    tray._presentation = TrayPresentation(
        "normal", "vBot", menu, TrayStatus("vBot", "", False, (), (), ())
    )
    order: list[str] = []
    user32 = windows_tray.native.user32
    for name in ("SetWindowPos", "SetForegroundWindow", "PostMessageW", "AllowSetForegroundWindow"):
        monkeypatch.setattr(user32, name, lambda *_args: True)
    monkeypatch.setattr(user32, "SetCursor", lambda _cursor: order.append("cursor"))

    def popup(*_args: object) -> int:
        order.append("popup")
        return 2

    monkeypatch.setattr(user32, "TrackPopupMenuEx", popup)

    tray._on_tray(windows_tray.native.WM_CONTEXTMENU, 100 | (200 << 16))

    assert order == ["cursor", "popup"]
    assert commands.calls == ["show_status"]
    assert tray._menu_open is False


def test_windows_toast_click_opens_it_and_switching_to_desktop_dismisses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    if sys.platform != "win32":
        pytest.skip("native Windows tray")
    from cli.application import windows_tray

    commands = Commands()
    tray, notified = _tray(commands, monkeypatch, tmp_path)
    monkeypatch.setattr(windows_tray.native.user32, "SetWinEventHook", lambda *_args: 7)
    monkeypatch.setattr(windows_tray.native.user32, "UnhookWinEvent", lambda _hook: True)
    monkeypatch.setattr(windows_tray.native.user32, "AllowSetForegroundWindow", lambda _v: True)
    toast = Toast("run:r1", "run_completed", "Coder finished", "Plan", session=("coder", "s1"))

    tray._show_toast(toast)
    tray._on_tray(0x405, 0)  # NIN_BALLOONUSERCLICK
    assert commands.calls == [toast]

    executable = {"name": "explorer.exe"}
    monkeypatch.setattr(windows_tray, "_executable_name", lambda _hwnd: executable["name"])
    tray._show_toast(toast)
    tray._on_foreground(7, 3, 99, 0, 0, 0, 0)
    assert tray._toast is toast
    executable["name"] = "vbot.desktop.exe"
    tray._on_foreground(7, 3, 99, 0, 0, 0, 0)
    assert tray._toast is None
    assert notified[-1] == (1, "")  # NIM_MODIFY with empty text removes the toast


def test_windows_tray_is_interacting_while_its_menu_toast_or_status_window_shows(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    if sys.platform != "win32":
        pytest.skip("native Windows tray")
    from cli.application import windows_status, windows_tray
    from cli.application.tray import TrayMenuItem, TrayStatus

    now = [0.0]
    tray, _notified = _tray(Commands(), monkeypatch, tmp_path, clock=lambda: now[0])
    user32 = windows_tray.native.user32
    for name in (
        "SetWindowPos",
        "SetForegroundWindow",
        "PostMessageW",
        "SetCursor",
        "SetWinEventHook",
        "UnhookWinEvent",
    ):
        monkeypatch.setattr(user32, name, lambda *_args: True)
    during_menu: list[bool] = []

    def popup(*_args: object) -> int:
        during_menu.append(tray.interacting())
        return 0

    monkeypatch.setattr(user32, "TrackPopupMenuEx", popup)
    tray._presentation = TrayPresentation(
        "normal",
        "vBot",
        (TrayMenuItem("Status…", "show_status"),),
        TrayStatus("vBot", "", False, (), (), ()),
    )
    assert tray.interacting() is False

    tray._on_tray(windows_tray.native.WM_CONTEXTMENU, 0)
    assert during_menu == [True]
    assert tray.interacting() is False

    toast = Toast("update:upd_1", "update_result", "vBot updated", "Update completed")
    tray.show_toast(toast)
    assert tray.interacting() is True  # before the UI thread shows it
    tray._drain()
    tray._on_tray(0x404, 0)  # NIN_BALLOONTIMEOUT
    assert tray.interacting() is False
    tray.show_toast(toast)
    tray._drain()
    now[0] += 300  # a toast whose close Windows never reports cannot block forever
    assert tray.interacting() is False

    status = windows_status.StatusWindow(lambda _action: None, lambda: 0, lambda: 0)
    monkeypatch.setattr(status, "_layout", lambda: None)
    tray._status = status
    assert tray.interacting() is False
    status.hwnd = 5
    status._handle(5, windows_tray.native.WM_SIZE, 0, 0)
    assert tray.interacting() is True
    status._handle(5, windows_tray.native.WM_SIZE, 1, 0)  # SIZE_MINIMIZED
    assert tray.interacting() is False
    status.hwnd = 0


@pytest.mark.parametrize("state", ["stopped", "busy", "error"])
def test_windows_icon_badges_the_state_and_keeps_the_normal_logo(state: str):
    if sys.platform != "win32":
        pytest.skip("native Windows tray")
    from PIL import Image

    from cli.application import windows_tray

    base = Image.new("RGBA", (64, 64), (255, 255, 255, 255))
    normal = windows_tray.render_icon(base, "normal", 32)
    badged = windows_tray.render_icon(base, state, 32)
    assert normal.getpixel((28, 28)) == (255, 255, 255, 255)
    assert badged.getpixel((28, 28))[:3] != (255, 255, 255)
    assert badged.getpixel((4, 4)) == (255, 255, 255, 255)
