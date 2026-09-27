"""Computer use: native Windows capture and input through a synthetic OS boundary."""

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
    # Input pacing needs no real time; stop tests restore the real wait explicitly.
    desktop._wait = lambda seconds: desktop._check()
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
        GetKeyState=lambda _: 0,
        MapVirtualKeyExW=lambda vk, mode, layout: vk,
        ToUnicodeEx=lambda *args: 1,
    )
    monitors = [
        {"id": 1, "x": -1280, "y": -100, "width": 1280, "height": 1024, "scale_percent": 100},
        {"id": 2, "x": 0, "y": 0, "width": 1920, "height": 1080, "scale_percent": 125},
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


def test_keyboard_text_maps_layout_modifiers_and_releases_between_characters(native):
    desktop, sent, _, _ = native
    mapping = {"g": 0x47, "z": 0x5A, "0": 0x30, ".": 0xBE, "4": 0x34, "A": 0x141, "@": 0x651}
    desktop.user.VkKeyScanExW = lambda char, layout: mapping.get(char, -1)
    desktop.capture({})
    desktop.input("type_text", {"text": "gz0.4A@", "text_mode": "keyboard"})
    assert [event[1] for event in sent if event[3] == 0] == [
        0x47,
        0x5A,
        0x30,
        0xBE,
        0x34,
        0x10,
        0x41,
        0x11,
        0x12,
        0x51,
    ]
    assert all(event[2] == 0 and event[3] in {0, 2} for event in sent)
    assert desktop._held == []


def test_keyboard_text_handles_caps_lock_without_toggling_user_state(native):
    desktop, sent, _, _ = native
    desktop.user.VkKeyScanExW = lambda char, layout: 0x141 if char == "A" else 0x41
    desktop.user.GetKeyState = lambda key: 1
    desktop.capture({})
    desktop.input("type_text", {"text": "Aa", "text_mode": "keyboard"})
    assert [event[1] for event in sent if event[3] == 0] == [0x41, 0x10, 0x41]


@pytest.mark.parametrize("text", ["a😀", "aЖ"])
def test_keyboard_text_unavailable_character_fails_before_any_character(native, text):
    desktop, sent, _, _ = native
    desktop.user.VkKeyScanExW = lambda char, layout: 0x41 if char == "a" else -1
    desktop.capture({})
    with pytest.raises(ComputerUseError) as caught:
        desktop.input("type_text", {"text": text, "text_mode": "keyboard"})
    assert caught.value.code == "unsupported_keyboard_text" and not sent


def test_keyboard_text_stop_releases_character_and_modifiers(native):
    desktop, sent, _, _ = native
    desktop.user.VkKeyScanExW = lambda char, layout: 0x141
    desktop.capture({})
    desktop._wait = lambda _: desktop.interrupt() or desktop._check()
    with pytest.raises(ComputerUseError):
        desktop.input("type_text", {"text": "AB", "text_mode": "keyboard"})
    assert desktop._held == []
    assert sent == [(1, 0x10, 0, 0), (1, 0x41, 0, 0), (1, 0x41, 0, 2), (1, 0x10, 0, 2)]


def test_keyboard_dead_key_preflight_does_not_send_prefix_or_change_composition(native):
    desktop, sent, _, _ = native
    desktop.user.VkKeyScanExW = lambda char, layout: 0x41 if char == "a" else 0xDC
    probes = []

    def translate(vk, scan, state, output, size, flags, layout):
        probes.append(flags)
        return -1 if vk == 0xDC else 1

    desktop.user.ToUnicodeEx = translate
    desktop.capture({})
    with pytest.raises(ComputerUseError) as caught:
        desktop.input("type_text", {"text": "a^", "text_mode": "keyboard"})
    assert caught.value.code == "unsupported_keyboard_text" and not sent
    assert probes == [4, 4]


@pytest.mark.parametrize(
    "name,args,held",
    [
        ("type_text", {"text": "abc", "text_mode": "keyboard"}, {0x11}),  # a modifier
        ("type_text", {"text": "abc", "text_mode": "keyboard"}, {0x41}),  # a key of the text
        ("press_key", {"key": "shift"}, {0x10}),
        ("click", {"x": 10, "y": 10, "modifiers": ["ctrl"]}, {0x11}),
    ],
)
def test_user_held_keys_refuse_input_without_sending_or_releasing(native, name, args, held):
    desktop, sent, _, _ = native
    desktop.capture({})
    desktop.user.GetAsyncKeyState = lambda key: 0x8000 if key in held else 0
    with pytest.raises(ComputerUseError) as caught:
        desktop.input(name, args)
    assert caught.value.code == "input_busy" and sent == []


@pytest.mark.parametrize("route", ["other_os", "background", "element"])
def test_keyboard_text_requires_supported_native_route_before_dispatch(native, monkeypatch, route):
    desktop, sent, _, _ = native
    client = CuaDriver.__new__(CuaDriver)
    client.desktop = None if route == "other_os" else desktop
    monkeypatch.setattr(client, "connect", lambda: None)
    args = {"text": "draft", "text_mode": "keyboard", "delivery_mode": "foreground"}
    if route == "background":
        args["delivery_mode"] = "background"
    if route == "element":
        args["element_token"] = "s00000001:1"
    with pytest.raises(ComputerUseError) as caught:
        client.call("type_text", args)
    assert caught.value.code == "unsupported_capability" and not sent


def test_mixed_dpi_coordinates_are_physical_and_scale_change_retires_capture(native):
    desktop, sent, monitors, _ = native
    monitors[0]["scale_percent"] = 100
    monitors[1]["scale_percent"] = 125
    args = {"session": "s", "monitor": 2}
    desktop.capture(args)
    desktop.input("move_cursor", {**args, "x": 800, "y": 400})
    assert sent[0][1:3] == (round(2080 * 65535 / 3199), round(500 * 65535 / 1179))
    monitors[1]["scale_percent"] = 150
    with pytest.raises(ComputerUseError) as caught:
        desktop.input("click", {**args, "x": 800, "y": 400})
    assert caught.value.code == "capture_required" and len(sent) == 1


def test_wrong_capture_pixel_dimensions_never_authorize_input(native, monkeypatch):
    desktop, sent, _, _ = native
    monkeypatch.setattr(
        "resources.extensions.computer_use.windows.ImageGrab.grab",
        lambda **kwargs: Image.new("RGB", (100, 100)),
    )
    with pytest.raises(ComputerUseError) as caught:
        desktop.capture({})
    assert caught.value.code == "capture_geometry_mismatch"
    assert not desktop._frames and not sent


def test_physical_dpi_context_restores_on_error_and_refuses_failed_entry():
    desktop = WindowsDesktop.__new__(WindowsDesktop)
    calls = []
    desktop.user = SimpleNamespace(
        SetThreadDpiAwarenessContext=lambda value: calls.append(value) or 42
    )
    with pytest.raises(RuntimeError), desktop._physical():
        raise RuntimeError("test-owned failure")
    assert calls[0].value == ct.c_void_p(-4).value and calls[1] == 42
    desktop.user.SetThreadDpiAwarenessContext = lambda value: None
    with pytest.raises(ComputerUseError) as caught, desktop._physical():
        pytest.fail("must not enter an unknown coordinate space")
    assert caught.value.code == "dpi_unavailable"


@pytest.mark.parametrize(
    "name,args,released",
    [
        ("hotkey", {"keys": ["ctrl", "shift"], "duration_ms": 2000}, [2]),
        (
            "drag",
            {
                "from_x": 10,
                "from_y": 10,
                "to_x": 500,
                "to_y": 500,
                "duration_ms": 2000,
                "modifiers": ["ctrl", "shift"],
            },
            # Mouse button up, then the modifiers in reverse order.
            [4, 2, 2],
        ),
    ],
)
def test_stop_interrupts_hold_and_drag_and_releases_every_owned_input(native, name, args, released):
    desktop, sent, _, _ = native
    desktop.capture({})
    waiting = threading.Event()
    real_wait = WindowsDesktop._wait.__get__(desktop)

    def wait(seconds):
        waiting.set()
        real_wait(seconds)

    desktop._wait = wait
    with ThreadPoolExecutor() as executor:
        future = executor.submit(desktop.input, name, args)
        assert waiting.wait(1)
        desktop.interrupt()
        with pytest.raises(ComputerUseError) as caught:
            future.result(timeout=0.5)
        assert caught.value.code == "computer_use_interrupted"
    assert desktop._held == []
    assert [event[3] for event in sent[-len(released) :]] == released
    if name == "drag":
        assert sent[-2:] == [(1, 0x10, 0, 2), (1, 0x11, 0, 2)]
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


def test_session_end_retires_geometry(native):
    desktop, sent, _, _ = native
    desktop.capture({"session": "s"})
    desktop.end_session("s")
    with pytest.raises(ComputerUseError) as caught:
        desktop.input("click", {"session": "s", "x": 1, "y": 1})
    assert caught.value.code == "capture_required" and sent == []


@pytest.mark.parametrize("foreign", [False, True])
def test_modal_window_resolution_never_uses_an_unrelated_foreground_window(
    native, monkeypatch, foreign
):
    desktop, sent, _, _ = native
    monkeypatch.setattr(desktop, "window_geometry", lambda args: (0, 0, 100, 100))
    desktop.user.IsWindowEnabled = lambda hwnd: hwnd != 10
    desktop.user.GetLastActivePopup = lambda hwnd: 20
    desktop.user.IsWindowVisible = lambda hwnd: True
    desktop.user.GetForegroundWindow = lambda: 900

    def process(hwnd, pointer):
        pointer._obj.value = 2 if foreign else 1
        return 1

    desktop.user.GetWindowThreadProcessId = process
    result = desktop.resolve_window({"pid": 1, "window_id": 10})
    assert result == {"pid": 1, "window_id": 10 if foreign else 20}
    assert sent == []


def test_background_dialog_blocks_input_before_cua_or_native_dispatch(native, monkeypatch):
    desktop, sent, _, _ = native
    client = CuaDriver.__new__(CuaDriver)
    client.desktop = desktop
    monkeypatch.setattr(client, "connect", lambda: None)
    monkeypatch.setattr(desktop, "resolve_window", lambda args: {"pid": 1, "window_id": 20})
    with pytest.raises(ComputerUseError) as caught:
        client.call(
            "type_text",
            {
                "pid": 1,
                "window_id": 10,
                "text": "must not type",
                "delivery_mode": "background",
            },
        )
    assert caught.value.code == "target_blocked" and sent == []


@pytest.mark.parametrize("switch_during_capture", [False, True])
def test_foreground_capture_never_labels_another_apps_pixels_as_the_target(
    native, monkeypatch, switch_during_capture
):
    desktop, sent, _, grabs = native
    monkeypatch.setattr(desktop, "_geometry", lambda args: ((0, 0, 100, 100), (0, 0, 100, 100)))
    calls = iter((10, 900) if switch_during_capture else (900,))
    desktop.user.GetForegroundWindow = lambda: next(calls)
    with pytest.raises(ComputerUseError) as caught:
        desktop.capture({"pid": 1, "window_id": 10})
    assert caught.value.code == "target_not_foreground"
    assert len(grabs) == int(switch_during_capture)
    assert not desktop._frames and sent == []
