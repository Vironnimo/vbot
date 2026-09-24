"""Live voice request delivery from the Desktop into the WebUI page."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import pytest

from desktop.live_requests import LiveRequestDispatcher, live_request_script


class FakeWindow:
    def __init__(self, result: Any = True, *, block: threading.Event | None = None) -> None:
        self.result = result
        self.block = block
        self.scripts: list[str] = []
        self.threads: list[threading.Thread] = []
        self.started = threading.Event()
        self.delivered = threading.Event()

    def evaluate_js(self, script: str) -> Any:
        self.threads.append(threading.current_thread())
        self.started.set()
        if self.block is not None:
            assert self.block.wait(timeout=5)
        self.scripts.append(script)
        self.delivered.set()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _detail(script: str) -> dict[str, str]:
    start = script.index("detail: ") + len("detail: ")
    detail: dict[str, str] = json.loads(script[start : script.index("}", start) + 1])
    return detail


def test_script_dispatches_a_cancelable_event_and_reports_handling() -> None:
    script = live_request_script("toggle", "hotkey")

    assert script.startswith('!window.dispatchEvent(new CustomEvent("vbot-desktop-live", ')
    assert "cancelable: true" in script
    assert _detail(script) == {"action": "toggle", "source": "hotkey"}


def test_request_is_delivered_on_a_background_thread() -> None:
    window = FakeWindow()
    dispatcher = LiveRequestDispatcher()
    dispatcher.attach_window(window)

    dispatcher.request("start", "wakeword")

    assert window.delivered.wait(timeout=2)
    assert window.threads[0] is not threading.current_thread()
    assert _detail(window.scripts[0]) == {"action": "start", "source": "wakeword"}
    dispatcher.close()


def test_request_never_blocks_the_producer_and_drops_excess() -> None:
    release = threading.Event()
    window = FakeWindow(block=release)
    dispatcher = LiveRequestDispatcher()
    dispatcher.attach_window(window)
    dispatcher.request("toggle", "hotkey")
    assert window.started.wait(timeout=2)

    # The page is still busy with the first request: the producer returns at
    # once, the bounded queue keeps four, and the rest are dropped.
    for _ in range(10):
        dispatcher.request("toggle", "hotkey")
    release.set()

    deadline = time.monotonic() + 2
    while len(window.scripts) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    dispatcher.close()
    assert len(window.scripts) == 5


def test_stale_requests_are_dropped() -> None:
    now = [100.0]
    started = threading.Event()
    release = threading.Event()

    class SlowWindow(FakeWindow):
        def evaluate_js(self, script: str) -> Any:
            started.set()
            assert release.wait(timeout=5)
            return super().evaluate_js(script)

    window = SlowWindow()
    dispatcher = LiveRequestDispatcher(clock=lambda: now[0])
    dispatcher.attach_window(window)
    dispatcher.request("start", "wakeword")
    assert started.wait(timeout=2)
    dispatcher.request("toggle", "hotkey")
    now[0] += 30.0
    release.set()

    dispatcher.close()
    assert [_detail(script)["source"] for script in window.scripts] == ["wakeword"]


@pytest.mark.parametrize(("action", "source"), [("stop", "hotkey"), ("start", "button")])
def test_invalid_requests_are_ignored(action: str, source: str) -> None:
    window = FakeWindow()
    dispatcher = LiveRequestDispatcher()
    dispatcher.attach_window(window)

    dispatcher.request(action, source)
    dispatcher.close()

    assert window.scripts == []


def test_missing_window_and_unhandled_page_are_only_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="vbot.desktop.live_requests")
    dispatcher = LiveRequestDispatcher(clock=lambda: 1.0)
    dispatcher._deliver("start", "wakeword", 1.0)

    window = FakeWindow(result=False)
    second = LiveRequestDispatcher()
    second.attach_window(window)
    second.request("toggle", "hotkey")
    assert window.delivered.wait(timeout=2)
    second.close()

    messages = [record.getMessage() for record in caplog.records]
    assert any("no window is attached" in message for message in messages)
    assert any("not handled by the page" in message for message in messages)


def test_page_errors_do_not_stop_delivery() -> None:
    window = FakeWindow(result=RuntimeError("page gone"))
    dispatcher = LiveRequestDispatcher()
    dispatcher.attach_window(window)

    dispatcher.request("start", "wakeword")
    assert window.delivered.wait(timeout=2)
    window.delivered.clear()
    window.result = True
    dispatcher.request("toggle", "hotkey")

    assert window.delivered.wait(timeout=2)
    dispatcher.close()
    assert len(window.scripts) == 2


def test_requests_after_close_are_ignored() -> None:
    window = FakeWindow()
    dispatcher = LiveRequestDispatcher()
    dispatcher.attach_window(window)
    dispatcher.close()

    dispatcher.request("start", "wakeword")

    assert window.scripts == []
