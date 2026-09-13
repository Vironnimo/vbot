"""The tray controller remains usable without its optional native backend."""

from __future__ import annotations

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
    actions = Actions(TrayState("stopped", "server"))
    controller = TrayController(actions, poll_interval=0.01)
    controller._poll_state()
    controller.start()
    try:
        actions.fail = "start_server"
        controller.invoke("start_server")
        _wait_for_call(actions, "start_server")
        assert "Could not start server: test failure" in next(iter(_labels(controller)))

        actions.fail = None
        controller.invoke("open_logs")
        _wait_for_call(actions, "open_logs")
    finally:
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
    assert "server stopped" in next(iter(menu))
    assert "Updated" in next(iter(menu))
    assert menu["Update"].enabled is True


def test_facade_startup_error_is_visible_without_removing_recovery_actions():
    actions = Actions(TrayState("stopped", "server", error="Startup failed: port is occupied"))
    controller = TrayController(actions)
    controller._poll_state()

    menu = _labels(controller)
    assert "Startup failed: port is occupied" in next(iter(menu))
    assert menu["Start server"].enabled is True
    assert menu["Update"].enabled is True
    assert menu["Open logs"].enabled is True


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
