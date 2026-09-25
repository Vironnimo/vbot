"""Live voice requests and Voice pushes from the Desktop into the page."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from desktop.page_events import (
    MAX_PENDING_VOICE_EVENTS,
    PageEventDispatcher,
    live_request_script,
    voice_push_script,
)


class FakeWindow:
    """pywebview window double; ``block`` holds every ``evaluate_js`` until set."""

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


def _live_detail(script: str) -> dict[str, str]:
    start = script.index("detail: ") + len("detail: ")
    detail: dict[str, str] = json.loads(script[start : script.index("}", start) + 1])
    return detail


def _voice_detail(script: str) -> dict[str, Any]:
    start = script.index("detail: ") + len("detail: ")
    assert script.endswith("}))")
    detail: dict[str, Any] = json.loads(script[start:-3])
    return detail


def _wait_until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.005)


@pytest.fixture
def dispatchers() -> Any:
    created: list[PageEventDispatcher] = []

    def create(**kwargs: Any) -> PageEventDispatcher:
        dispatcher = PageEventDispatcher(**kwargs)
        created.append(dispatcher)
        return dispatcher

    yield create
    for dispatcher in created:
        dispatcher.close()


# -- Scripts -------------------------------------------------------------------


def test_live_script_dispatches_a_cancelable_event_and_reports_handling() -> None:
    script = live_request_script("toggle", "hotkey")

    assert script.startswith('!window.dispatchEvent(new CustomEvent("vbot-desktop-live", ')
    assert "cancelable: true" in script
    assert _live_detail(script) == {"action": "toggle", "source": "hotkey"}


def test_voice_script_dispatches_the_detail_as_json() -> None:
    detail = {"type": "event", "event": {"sequence": 3, "kind": "sent", "note": "</script>"}}

    script = voice_push_script(detail)

    assert script.startswith('window.dispatchEvent(new CustomEvent("vbot-desktop-voice", ')
    assert _voice_detail(script) == detail


# -- Live voice requests ---------------------------------------------------------


def test_live_request_is_delivered_on_a_background_thread(dispatchers: Any) -> None:
    window = FakeWindow()
    dispatcher = dispatchers()
    dispatcher.attach_window(window)

    dispatcher.request_live("start", "wakeword")

    assert window.delivered.wait(timeout=2)
    assert window.threads[0] is not threading.current_thread()
    assert window.threads[0].name == "vbot-desktop-page-events"
    assert window.threads[0].daemon is True
    assert _live_detail(window.scripts[0]) == {"action": "start", "source": "wakeword"}


def test_live_requests_never_block_the_producer_and_drop_excess(dispatchers: Any) -> None:
    release = threading.Event()
    window = FakeWindow(block=release)
    dispatcher = dispatchers()
    dispatcher.attach_window(window)
    dispatcher.request_live("toggle", "hotkey")
    assert window.started.wait(timeout=2)

    # The page is still busy with the first request: the producer returns at
    # once, four requests wait, and the rest are dropped.
    for _ in range(10):
        dispatcher.request_live("toggle", "hotkey")
    release.set()

    _wait_until(lambda: len(window.scripts) == 5)
    time.sleep(0.05)
    assert len(window.scripts) == 5


def test_stale_live_requests_are_dropped(dispatchers: Any) -> None:
    now = [100.0]
    release = threading.Event()
    window = FakeWindow(block=release)
    dispatcher = dispatchers(clock=lambda: now[0])
    dispatcher.attach_window(window)
    dispatcher.request_live("start", "wakeword")
    assert window.started.wait(timeout=2)
    dispatcher.request_live("toggle", "hotkey")  # waits, then goes stale
    now[0] += 30.0
    dispatcher.request_live("start", "wakeword")  # fresh
    release.set()

    _wait_until(lambda: len(window.scripts) == 2)
    assert [_live_detail(script) for script in window.scripts] == [
        {"action": "start", "source": "wakeword"},
        {"action": "start", "source": "wakeword"},
    ]


@pytest.mark.parametrize(("action", "source"), [("stop", "hotkey"), ("start", "button")])
def test_invalid_live_requests_are_ignored(dispatchers: Any, action: str, source: str) -> None:
    window = FakeWindow()
    dispatcher = dispatchers()
    dispatcher.attach_window(window)

    dispatcher.request_live(action, source)
    dispatcher.close()

    assert window.scripts == []


def test_missing_window_and_unhandled_page_are_only_logged(
    dispatchers: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="vbot.desktop.page_events")
    without_window = dispatchers()
    without_window.request_live("start", "wakeword")
    window = FakeWindow(result=False)
    unhandled = dispatchers()
    unhandled.attach_window(window)
    unhandled.request_live("toggle", "hotkey")

    def logged(fragment: str) -> bool:
        return any(fragment in record.getMessage() for record in caplog.records)

    _wait_until(lambda: logged("no window is attached") and logged("not handled by the page"))


def test_page_errors_do_not_stop_delivery(dispatchers: Any) -> None:
    window = FakeWindow(result=RuntimeError("page gone"))
    dispatcher = dispatchers()
    dispatcher.attach_window(window)

    dispatcher.request_live("start", "wakeword")
    dispatcher.publish_event({"sequence": 1, "kind": "detected"})
    _wait_until(lambda: len(window.scripts) == 2)
    window.result = True
    dispatcher.request_live("toggle", "hotkey")

    _wait_until(lambda: len(window.scripts) == 3)


def test_pushes_after_close_are_ignored(dispatchers: Any) -> None:
    window = FakeWindow()
    dispatcher = dispatchers()
    dispatcher.attach_window(window)
    dispatcher.close()

    dispatcher.request_live("start", "wakeword")
    dispatcher.publish_status({"sequence": 1})
    dispatcher.publish_event({"sequence": 2, "kind": "detected"})

    time.sleep(0.05)
    assert window.scripts == []


# -- Voice pushes ------------------------------------------------------------------


def test_voice_pushes_keep_their_order(dispatchers: Any) -> None:
    window = FakeWindow()
    dispatcher = dispatchers()
    dispatcher.attach_window(window)

    dispatcher.publish_event({"sequence": 1, "kind": "detected"})
    dispatcher.publish_status({"sequence": 2, "state": "listening"})
    dispatcher.publish_event({"sequence": 3, "kind": "recording_started"})

    _wait_until(lambda: len(window.scripts) == 3)
    assert [_voice_detail(script) for script in window.scripts] == [
        {"type": "event", "event": {"sequence": 1, "kind": "detected"}},
        {"type": "status", "status": {"sequence": 2, "state": "listening"}},
        {"type": "event", "event": {"sequence": 3, "kind": "recording_started"}},
    ]


def test_a_waiting_status_is_replaced_by_the_newest_snapshot(dispatchers: Any) -> None:
    release = threading.Event()
    window = FakeWindow(block=release)
    dispatcher = dispatchers()
    dispatcher.attach_window(window)
    dispatcher.publish_status({"sequence": 1})
    assert window.started.wait(timeout=2)

    dispatcher.publish_status({"sequence": 2})
    dispatcher.publish_event({"sequence": 3, "kind": "detected"})
    dispatcher.publish_status({"sequence": 4})
    release.set()

    _wait_until(lambda: len(window.scripts) == 3)
    time.sleep(0.05)
    # The waiting snapshot keeps its place in the queue but carries the newest content.
    assert [_voice_detail(script) for script in window.scripts] == [
        {"type": "status", "status": {"sequence": 1}},
        {"type": "status", "status": {"sequence": 4}},
        {"type": "event", "event": {"sequence": 3, "kind": "detected"}},
    ]


def test_waiting_events_are_bounded_by_dropping_the_oldest(dispatchers: Any) -> None:
    release = threading.Event()
    window = FakeWindow(block=release)
    dispatcher = dispatchers()
    dispatcher.attach_window(window)
    dispatcher.publish_event({"sequence": 0, "kind": "detected"})
    assert window.started.wait(timeout=2)

    for sequence in range(1, MAX_PENDING_VOICE_EVENTS + 11):
        dispatcher.publish_event({"sequence": sequence, "kind": "detected"})
    dispatcher.request_live("start", "wakeword")
    release.set()

    _wait_until(lambda: len(window.scripts) == MAX_PENDING_VOICE_EVENTS + 2)
    sequences = [
        _voice_detail(script)["event"]["sequence"]
        for script in window.scripts
        if "vbot-desktop-voice" in script
    ]
    # The first was in flight; the ten oldest waiting events were dropped.
    assert sequences == [0, *range(11, MAX_PENDING_VOICE_EVENTS + 11)]
    assert "vbot-desktop-live" in window.scripts[-1]


def test_an_unserializable_push_is_dropped_and_delivery_continues(
    dispatchers: Any, caplog: pytest.LogCaptureFixture
) -> None:
    window = FakeWindow()
    dispatcher = dispatchers()
    dispatcher.attach_window(window)

    with caplog.at_level(logging.ERROR, logger="vbot.desktop.page_events"):
        dispatcher.publish_event({"sequence": 1, "kind": "detected", "bad": object()})
        dispatcher.publish_event({"sequence": 2, "kind": "sent"})
        _wait_until(lambda: len(window.scripts) == 1)

    assert _voice_detail(window.scripts[0])["event"]["sequence"] == 2
    assert "not serializable" in caplog.text


def test_close_stops_the_delivery_thread(dispatchers: Any) -> None:
    window = FakeWindow()
    dispatcher = dispatchers()
    dispatcher.attach_window(window)
    dispatcher.publish_status({"sequence": 1})
    assert window.delivered.wait(timeout=2)
    thread = window.threads[0]

    dispatcher.close()

    assert not thread.is_alive()
