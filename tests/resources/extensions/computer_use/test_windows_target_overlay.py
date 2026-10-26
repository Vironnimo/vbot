"""The activity sign's native lifecycle, with an in-memory Windows message queue."""

# The fake implements the Windows API's names.
# ruff: noqa: N802

from __future__ import annotations

import ctypes as ct
import queue
import threading
from types import SimpleNamespace

import pytest

from resources.extensions.computer_use import _win32, _win_overlay
from resources.extensions.computer_use.target import TargetError


class OverlayDesktop:
    """Native resources and z-order, without a real window, clock delay or input."""

    def __init__(self, monkeypatch):
        self.messages = queue.SimpleQueue()
        self.classes = set()
        self.windows = {}
        self.stack = []
        self.hooks = {}
        self.positions = []
        self.paints = []
        self.next_handle = 100
        self.started = threading.Event()
        self.start_gate = threading.Event()
        self.start_gate.set()
        self.fail_hook = False
        self.api = SimpleNamespace(
            user32=self,
            kernel32=self,
            window_proc=ct.CFUNCTYPE(
                ct.c_ssize_t, ct.c_void_p, ct.c_uint, ct.c_size_t, ct.c_ssize_t
            ),
            event_proc=lambda callback: callback,
        )
        monkeypatch.setattr(_win32, "api", lambda: self.api)
        monkeypatch.setattr(_win32, "enter_thread", lambda: None)
        monkeypatch.setattr(_win32, "monitors", self.monitors)
        monkeypatch.setattr(_win32, "top_level_windows", lambda: list(self.stack))
        monkeypatch.setattr(_win32, "is_visible", lambda handle: True)
        monkeypatch.setattr(_win32, "is_cloaked", lambda handle: False)
        monkeypatch.setattr(_win32, "window_class", lambda handle: "OtherApp")
        monkeypatch.setattr(_win32, "paint_layered", self.paint)
        monkeypatch.setattr(_win32, "set_layered_opacity", lambda *_: True)
        monkeypatch.setattr(_win_overlay, "sys", SimpleNamespace(platform="win32"))
        monkeypatch.setattr(_win_overlay, "time", SimpleNamespace(sleep=lambda _: None))
        monkeypatch.setattr(_win_overlay, "_FADE_STEP", _win_overlay._FADE_SECONDS)

    def monitors(self):
        return [_win32.Monitor("display", 0, 0, 640, 480, True, 96)]

    def idle(self):
        # A native callback can enqueue one restack behind the first barrier.
        for _ in range(2):
            reached = threading.Event()
            self.messages.put(reached.set)
            assert reached.wait(1)

    def event(self, event, handle, object_id=0, child_id=0):
        def dispatch():
            for hook, (first, last, callback) in list(self.hooks.items()):
                if first <= event <= last:
                    callback(hook, event, handle, object_id, child_id, 2, 0)

        self.messages.put(dispatch)

    def GetModuleHandleW(self, _name):
        return 1

    def GetCurrentThreadId(self):
        return 7

    def GetDesktopWindow(self):
        return 1

    def PeekMessageW(self, *_args):
        self.messages = queue.SimpleQueue()
        self.started.set()
        assert self.start_gate.wait(1)
        return 0

    def GetMessageW(self, message, *_args):
        while callable(item := self.messages.get()):
            item()
        message._obj.hwnd = None
        message._obj.message = item
        return 0 if item == _win_overlay._WM_QUIT else 1

    def PostThreadMessageW(self, _thread, message, *_args):
        self.messages.put(message)
        return 1

    def DispatchMessageW(self, _message):
        return 0

    def RegisterClassExW(self, info):
        name = info._obj.class_name
        assert name not in self.classes
        self.classes.add(name)
        return 1

    def UnregisterClassW(self, name, _instance):
        assert name not in self.windows.values()
        self.classes.remove(name)
        return 1

    def CreateWindowExW(self, style, name, *_args):
        assert name in self.classes
        assert style & _win32.WS_EX_NOACTIVATE
        assert style & _win32.WS_EX_TRANSPARENT
        self.next_handle += 1
        handle = self.next_handle
        self.windows[handle] = name
        self.stack.insert(0, handle)
        return handle

    def DestroyWindow(self, handle):
        del self.windows[handle]
        self.stack.remove(handle)
        return 1

    def SetWindowDisplayAffinity(self, _handle, affinity):
        assert affinity == 0x11
        return 1

    def ShowWindow(self, _handle, command):
        assert command == 4  # never activate
        return 1

    def paint(self, handle, *_args):
        self.paints.append(handle)
        return True

    def SetWindowPos(self, handle, after, _x, _y, _w, _h, flags):
        assert after == -1 and flags & 0x10  # topmost without activation
        self.positions.append(handle)
        self.stack.remove(handle)
        self.stack.insert(0, handle)
        return 1

    def SetWinEventHook(self, first, last, _module, callback, _process, _thread, flags):
        assert flags == 1  # skip our own thread's z-order events
        if self.fail_hook and self.hooks:
            return 0
        self.next_handle += 1
        self.hooks[self.next_handle] = (first, last, callback)
        return self.next_handle

    def UnhookWinEvent(self, handle):
        del self.hooks[handle]
        return 1


@pytest.mark.parametrize(
    ("event", "handle", "object_id"),
    [(0x3, 2, 0), (0x8002, 2, 0), (0x800B, 2, 0), (0x8004, 1, -4)],
    ids=["foreground", "shown", "maximized", "desktop-z-order"],
)
def test_activity_recovers_when_covered_without_repainting_or_repeated_raising(
    monkeypatch, event, handle, object_id
):
    desktop = OverlayDesktop(monkeypatch)
    overlay = _win_overlay.ActivityOverlay()
    try:
        overlay.show()
        assert desktop.started.wait(1)
        desktop.idle()
        own = set(desktop.windows)
        assert len(own) == 5
        painted = list(desktop.paints)
        desktop.positions.clear()
        desktop.stack.insert(0, 2)  # a new topmost application covers the sign

        # Carets, controls and the sign itself do not trigger z-order changes.
        desktop.event(0x800B, 2, -8)
        desktop.event(0x8004, 2, -4)
        desktop.event(0x8002, 2, 0, 1)
        desktop.event(0x800B, next(iter(own)))
        desktop.idle()
        assert desktop.positions == []

        desktop.event(event, handle, object_id)
        desktop.idle()
        assert set(desktop.stack[:5]) == own
        assert set(desktop.positions) == own
        raised = list(desktop.positions)
        desktop.event(event, handle, object_id)
        overlay.show()
        desktop.idle()
        assert desktop.positions == raised
        assert desktop.paints == painted
    finally:
        overlay.close()
    assert not desktop.windows and not desktop.hooks and not desktop.classes


def test_activity_release_destroys_resources_and_can_start_again(monkeypatch):
    desktop = OverlayDesktop(monkeypatch)
    for _ in range(2):  # a fresh owner, as after Extension Reload
        overlay = _win_overlay.ActivityOverlay()
        try:
            for _ in range(2):
                overlay.show()
                assert desktop.started.wait(1)
                desktop.idle()
                assert len(desktop.windows) == 5 and desktop.hooks and desktop.classes
                overlay.hide()
                desktop.idle()
                assert not desktop.windows and not desktop.hooks and not desktop.classes
                raised = list(desktop.positions)
                desktop.event(0x8004, 1, -4)
                desktop.idle()
                assert desktop.positions == raised
        finally:
            overlay.close()
            desktop.started.clear()
        assert not overlay._thread.is_alive()
        overlay.show()
        assert not desktop.windows  # a closed owner cannot resurrect the sign


def test_activity_close_during_startup_leaves_no_thread_or_resources(monkeypatch):
    desktop = OverlayDesktop(monkeypatch)
    desktop.start_gate.clear()
    overlay = _win_overlay.ActivityOverlay()
    closing = threading.Event()
    post = overlay._post

    def track_close(message):
        post(message)
        if message == _win_overlay._WM_QUIT:
            closing.set()

    monkeypatch.setattr(overlay, "_post", track_close)
    overlay.show()  # must return while the native message queue is still starting
    assert desktop.started.wait(1)
    closer = threading.Thread(target=overlay.close)
    try:
        closer.start()
        assert closing.wait(1)
    finally:
        desktop.start_gate.set()
        closer.join(1)
    assert not closer.is_alive() and not overlay._thread.is_alive()
    assert not desktop.windows and not desktop.hooks and not desktop.classes


def test_activity_resumed_during_fade_out_keeps_window_tracking(monkeypatch):
    desktop = OverlayDesktop(monkeypatch)
    overlay = _win_overlay.ActivityOverlay()

    def resume(_handle, alpha):
        if alpha == 0:
            overlay.show()
        return True

    try:
        overlay.show()
        assert desktop.started.wait(1)
        desktop.idle()
        own = set(desktop.windows)
        monkeypatch.setattr(_win32, "set_layered_opacity", resume)
        overlay.hide()
        desktop.idle()
        assert set(desktop.windows) == own and desktop.hooks
        desktop.stack.insert(0, 2)
        desktop.event(0x8004, 1, -4)
        desktop.idle()
        assert set(desktop.stack[:5]) == own
    finally:
        overlay.close()
    assert not desktop.windows and not desktop.hooks and not desktop.classes


@pytest.mark.parametrize("failure", ["hook", "layout"])
def test_activity_initialization_failure_cleans_up_and_allows_retry(monkeypatch, failure):
    desktop = OverlayDesktop(monkeypatch)
    overlay = _win_overlay.ActivityOverlay()
    if failure == "hook":
        desktop.fail_hook = True  # fails after windows and one hook already exist
    else:

        def failed_monitors():
            raise TargetError("test display failure")

        monkeypatch.setattr(_win32, "monitors", failed_monitors)
    try:
        overlay.show()
        overlay._thread.join(1)
        assert not overlay._thread.is_alive()
        assert not desktop.windows and not desktop.hooks and not desktop.classes
        failed_thread = overlay._thread
        overlay.hide()
        assert overlay._thread is failed_thread and not failed_thread.is_alive()
        desktop.fail_hook = False
        monkeypatch.setattr(_win32, "monitors", desktop.monitors)
        desktop.started.clear()
        overlay.show()
        assert desktop.started.wait(1)
        desktop.idle()
        assert len(desktop.windows) == 5
    finally:
        overlay.close()
    assert not desktop.windows and not desktop.hooks and not desktop.classes
