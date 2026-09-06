"""Native desktop regressions; synthetic OS boundary never sends user input."""

from __future__ import annotations

import ctypes as ct
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from PIL import Image

from resources.extensions.computer_use.driver import ComputerUseError, CuaDriver
from resources.extensions.computer_use.windows import Input, WindowsDesktop


@pytest.fixture
def native(monkeypatch):
    desktop = WindowsDesktop.__new__(WindowsDesktop)
    desktop._stopped = threading.Event()
    desktop._lock = threading.RLock()
    desktop._held = []
    desktop._frames = {}
    sent = []

    def send(count, events, size):
        assert size == ct.sizeof(Input)
        sent.extend(
            [
                (event.type, event.key.vk, event.key.scan, event.key.flags)
                if event.type
                else (0, event.mouse.dx, event.mouse.dy, event.mouse.flags, event.mouse.data)
                for event in events
            ]
        )
        return count

    desktop.user = SimpleNamespace(
        SendInput=send,
        GetForegroundWindow=lambda: 1,
        GetWindowThreadProcessId=lambda *args: 1,
        GetKeyboardLayout=lambda _: 1,
        VkKeyScanExW=lambda char, _: ord(char.upper()) if char.isalpha() else -1,
        GetAsyncKeyState=lambda _: 0,
    )
    monitors = [
        {"id": 1, "x": -1280, "y": -100, "width": 1280, "height": 1024},
        {"id": 2, "x": 0, "y": 0, "width": 1920, "height": 1080},
    ]
    monkeypatch.setattr(desktop, "monitors", lambda: monitors)
    monkeypatch.setattr(desktop, "_physical", nullcontext)
    grabs = []

    def grab(**kwargs):
        grabs.append(kwargs)
        left, top, right, bottom = kwargs["bbox"]
        return Image.new("RGB", (right - left, bottom - top))

    monkeypatch.setattr("resources.extensions.computer_use.windows.ImageGrab.grab", grab)
    return desktop, sent, monitors, grabs


def test_capture_all_monitors_and_selected_display_preserves_negative_origin(native):
    desktop, _, _, grabs = native
    result = desktop.capture({"session": "s"})
    assert result["screen_origin"] == [-1280, -100]
    assert grabs[-1] == {
        "bbox": (-1280, -100, 1920, 1080),
        "all_screens": True,
        "include_layered_windows": True,
    }
    desktop.capture({"session": "s", "monitor": 1})
    assert grabs[-1]["bbox"] == (-1280, -100, 0, 924)


def test_selected_monitor_coordinates_map_to_virtual_desktop(native):
    desktop, sent, _, _ = native
    args = {"session": "s", "monitor": 1}
    desktop.capture(args)
    desktop.input("click", {**args, "x": 100, "y": 50})
    assert sent[0] == (0, round(100 * 65535 / 3199), round(50 * 65535 / 1179), 0xC001, 0)
    assert [event[3] for event in sent[1:]] == [2, 4]


def test_gap_between_displays_refuses_instead_of_snapping_pointer(native):
    desktop, sent, _, _ = native
    desktop.capture({})
    with pytest.raises(ComputerUseError) as caught:
        desktop.input("click", {"x": 50, "y": 1150})
    assert caught.value.code == "invalid_coordinates"
    assert sent == []


def test_layout_change_or_foreign_session_refuses_before_input(native):
    desktop, sent, monitors, _ = native
    desktop.capture({"session": "s"})
    with pytest.raises(ComputerUseError, match="Capture"):
        desktop.input("click", {"session": "other", "x": 1, "y": 1})
    monitors[0]["width"] += 1
    with pytest.raises(ComputerUseError, match="Capture"):
        desktop.input("click", {"session": "s", "x": 1, "y": 1})
    assert sent == []


def test_unicode_uses_utf16_without_clipboard_or_keyboard_layout(native):
    desktop, sent, _, _ = native
    desktop.capture({})
    desktop.input("type_text", {"text": "äöüß€😀\n\t"})
    scans = [event[2] for event in sent if event[3] == 4]
    assert scans == [0xE4, 0xF6, 0xFC, 0xDF, 0x20AC, 0xD83D, 0xDE00]
    assert sent[-4:] == [(1, 13, 0, 0), (1, 13, 0, 2), (1, 9, 0, 0), (1, 9, 0, 2)]


@pytest.mark.parametrize(
    "name,args,release_flag",
    [
        ("hotkey", {"keys": ["ctrl", "shift"], "duration_ms": 2000}, 2),
        ("drag", {"from_x": 10, "from_y": 10, "to_x": 500, "to_y": 500, "duration_ms": 2000}, 4),
    ],
)
def test_stop_interrupts_hold_and_drag_and_releases_every_owned_input(
    native, name, args, release_flag
):
    desktop, sent, _, _ = native
    desktop.capture({})
    waiting = threading.Event()
    original_wait = desktop._wait

    def wait(seconds):
        waiting.set()
        original_wait(seconds)

    desktop._wait = wait
    with ThreadPoolExecutor() as executor:
        future = executor.submit(desktop.input, name, args)
        assert waiting.wait(1)
        desktop.interrupt()
        with pytest.raises(ComputerUseError) as caught:
            future.result(timeout=0.5)
        assert caught.value.code == "computer_use_stopped"
    assert desktop._held == []
    assert sent[-1][3] == release_flag
    previous = list(sent)
    with pytest.raises(ComputerUseError):
        desktop.input("type_text", {"text": "never"})
    assert sent == previous


def test_partial_send_failure_releases_modifier_and_never_replays(native):
    desktop, sent, _, _ = native
    desktop.capture({})
    original = desktop.user.SendInput
    calls = 0

    def send(count, events, size):
        nonlocal calls
        calls += 1
        if calls == 2:
            return 0
        return original(count, events, size)

    desktop.user.SendInput = send
    with pytest.raises(ComputerUseError) as caught:
        desktop.input("hotkey", {"keys": ["ctrl", "a"]})
    assert caught.value.code == "input_refused"
    assert desktop._held == []
    assert sent[-1] == (1, 0x11, 0, 2)
    assert calls == 3


def test_user_held_key_is_not_released_by_agent(native):
    desktop, sent, _, _ = native
    desktop.capture({})
    desktop.user.GetAsyncKeyState = lambda key: 0x8000
    with pytest.raises(ComputerUseError) as caught:
        desktop.input("press_key", {"key": "shift"})
    assert caught.value.code == "input_busy"
    assert sent == []


def test_capture_layout_race_never_authorizes_input(native, monkeypatch):
    desktop, sent, monitors, _ = native

    def grab(**kwargs):
        monitors[0]["x"] -= 20
        return Image.new("RGB", (3200, 1180))

    monkeypatch.setattr("resources.extensions.computer_use.windows.ImageGrab.grab", grab)
    with pytest.raises(ComputerUseError) as caught:
        desktop.capture({})
    assert caught.value.code == "stale_view"
    assert not desktop._frames and not sent


def test_pixel_capture_and_input_never_call_mcp(native, monkeypatch):
    desktop, sent, _, _ = native
    client = CuaDriver.__new__(CuaDriver)
    client.desktop = desktop
    monkeypatch.setattr(client, "connect", lambda: None)
    client.call("get_desktop_state", {"session": "s"})
    client.call("move_cursor", {"session": "s", "delivery_mode": "foreground", "x": 10, "y": 10})
    assert len(sent) == 1
    assert client.call("list_monitors", {})["monitors"][0]["x"] == -1280


def test_native_input_abi_matches_windows_x64():
    if ct.sizeof(ct.c_void_p) == 8:
        assert ct.sizeof(Input) == 40


def test_window_pixels_are_captured_after_accessibility_query(monkeypatch):
    client = CuaDriver.__new__(CuaDriver)
    order = []

    def capture(args):
        order.append("pixels")
        return {"screen_origin": [0, 0]}

    def query(function, name, args):
        assert name == "get_window_state" and args["include_screenshot"] is False
        order.append("elements")
        return SimpleNamespace(model_dump=lambda **kwargs: {"structuredContent": {"elements": []}})

    client.desktop = SimpleNamespace(capture=capture)
    client._portal = SimpleNamespace(call=query)
    client._session = SimpleNamespace(call_tool=object())
    client.schemas = {"get_window_state": {}}
    monkeypatch.setattr(client, "connect", lambda: None)
    result = client.call("get_window_state", {"pid": 1, "window_id": 2})
    assert order == ["elements", "pixels"]
    assert result["screen_origin"] == [0, 0] and result["elements"] == []


@pytest.mark.parametrize(
    "name,args",
    [
        ("click", {"x": 10, "y": 10, "count": 2}),
        ("scroll", {"x": 10, "y": 10, "direction": "down"}),
        ("drag", {"from_x": 10, "from_y": 10, "to_x": 30, "to_y": 30, "duration_ms": 0}),
    ],
)
def test_modifiers_span_entire_pointer_action_and_release(native, name, args):
    desktop, sent, _, _ = native
    desktop.capture({})
    desktop.input(name, {**args, "modifiers": ["ctrl", "shift"]})
    assert sent[:2] == [(1, 0x11, 0, 0), (1, 0x10, 0, 0)]
    assert sent[-2:] == [(1, 0x10, 0, 2), (1, 0x11, 0, 2)]
    assert all(event[0] == 0 for event in sent[2:-2])
    assert not desktop._held


def test_modified_drag_stop_releases_mouse_and_modifiers(native):
    desktop, sent, _, _ = native
    desktop.capture({})
    waiting = threading.Event()
    original_wait = desktop._wait

    def wait(seconds):
        waiting.set()
        original_wait(seconds)

    desktop._wait = wait
    with ThreadPoolExecutor() as executor:
        future = executor.submit(
            desktop.input,
            "drag",
            {
                "from_x": 10,
                "from_y": 10,
                "to_x": 500,
                "to_y": 500,
                "duration_ms": 2000,
                "modifiers": ["ctrl", "shift"],
            },
        )
        assert waiting.wait(1)
        desktop.interrupt()
        with pytest.raises(ComputerUseError):
            future.result(timeout=0.5)
    assert sent[-3][3] == 4
    assert sent[-2:] == [(1, 0x10, 0, 2), (1, 0x11, 0, 2)]
    assert not desktop._held


def test_user_held_pointer_modifier_refuses_before_moving(native):
    desktop, sent, _, _ = native
    desktop.capture({})
    desktop.user.GetAsyncKeyState = lambda _: 0x8000
    with pytest.raises(ComputerUseError) as caught:
        desktop.input("click", {"x": 10, "y": 10, "modifiers": ["ctrl"]})
    assert caught.value.code == "input_busy" and not sent


def test_session_end_retires_geometry(native):
    desktop, _, _, _ = native
    desktop.capture({"session": "s"})
    desktop.end_session("s")
    assert not desktop._frames


@pytest.mark.parametrize("flags", [0x10, 0x02, 0x12])
def test_emergency_stop_ignores_injected_escape(flags):
    from resources.extensions.computer_use.driver import EmergencyHotkey

    hotkey = EmergencyHotkey(lambda: None)
    hotkey.set_armed(True)
    for _ in range(2):
        hotkey._key_event(0x1B, True, flags)
        hotkey._key_event(0x1B, False, flags)
    assert not hotkey.pending


def test_emergency_stop_needs_two_separate_physical_presses(monkeypatch):
    from resources.extensions.computer_use import driver

    now = [10.0]
    monkeypatch.setattr(driver.time, "monotonic", lambda: now[0])
    hotkey = driver.EmergencyHotkey(lambda: None)
    hotkey.set_armed(True)
    hotkey._key_event(0x1B, True, 0)
    now[0] += 0.2
    hotkey._key_event(0x1B, True, 0)  # OS auto-repeat.
    assert not hotkey.pending
    hotkey._key_event(0x1B, False, 0)
    hotkey._key_event(0x1B, True, 0)
    assert hotkey.pending


@pytest.mark.parametrize("between", ["timeout", "other_key", "disarm", "inactive"])
def test_emergency_stop_does_not_join_unrelated_presses(monkeypatch, between):
    from resources.extensions.computer_use import driver

    now = [10.0]
    monkeypatch.setattr(driver.time, "monotonic", lambda: now[0])
    hotkey = driver.EmergencyHotkey(lambda: None)
    hotkey.set_armed(between != "inactive")
    hotkey._key_event(0x1B, True, 0)
    hotkey._key_event(0x1B, False, 0)
    if between == "timeout":
        now[0] += 0.7
    elif between == "other_key":
        hotkey._key_event(0x41, True, 0)
    elif between == "disarm":
        hotkey.set_armed(False)
        hotkey.set_armed(True)
    else:
        hotkey.set_armed(True)
    hotkey._key_event(0x1B, True, 0)
    assert not hotkey.pending


def test_emergency_stop_dispatch_never_blocks_keyboard_listener():
    from resources.extensions.computer_use.driver import EmergencyHotkey

    entered = threading.Event()
    release = threading.Event()

    def stop():
        entered.set()
        assert release.wait(1)

    hotkey = EmergencyHotkey(stop)
    hotkey._worker = threading.Thread(target=hotkey._dispatch)
    hotkey._worker.start()
    try:
        hotkey.set_armed(True)
        hotkey._key_event(0x1B, True, 0)
        hotkey._key_event(0x1B, False, 0)
        hotkey._key_event(0x1B, True, 0)
        assert entered.wait(0.5)
        assert hotkey.pending
        # The listener can still process input while interruption is draining.
        hotkey._key_event(0x41, True, 0)
    finally:
        release.set()
        hotkey.close()
    assert not hotkey._worker.is_alive()
