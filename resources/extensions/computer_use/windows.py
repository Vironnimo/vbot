"Physical Windows desktop capture and interruptible input, without build dependencies."

from __future__ import annotations

import base64
import ctypes as ct
import io
import threading
import time
from contextlib import contextmanager
from typing import Any

from PIL import ImageGrab

from .driver import ComputerUseError


class Rect(ct.Structure):
    _fields_ = [(name, ct.c_int32) for name in ("left", "top", "right", "bottom")]


class MouseInput(ct.Structure):
    _fields_ = [
        ("dx", ct.c_int32),
        ("dy", ct.c_int32),
        ("data", ct.c_uint32),
        ("flags", ct.c_uint32),
        ("time", ct.c_uint32),
        ("extra", ct.c_size_t),
    ]


class KeyInput(ct.Structure):
    _fields_ = [
        ("vk", ct.c_uint16),
        ("scan", ct.c_uint16),
        ("flags", ct.c_uint32),
        ("time", ct.c_uint32),
        ("extra", ct.c_size_t),
    ]


class InputData(ct.Union):
    _fields_ = [("mouse", MouseInput), ("key", KeyInput)]


class Input(ct.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ct.c_uint32), ("value", InputData)]


KEYS = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "windows": 0x5B,
    "super": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 9,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "backspace": 8,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "capslock": 0x14,
    "pause": 0x13,
    "printscreen": 0x2C,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}
BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


def keyboard(vk: int, *, up: bool = False, scan: int = 0) -> Input:
    flags = (2 if up else 0) | (4 if scan else (1 if vk in EXTENDED else 0))
    return Input(type=1, key=KeyInput(vk=vk, scan=scan, flags=flags))


def mouse(flags: int, *, x: int = 0, y: int = 0, data: int = 0) -> Input:
    return Input(type=0, mouse=MouseInput(dx=x, dy=y, data=data & 0xFFFFFFFF, flags=flags))


class WindowsDesktop:
    "Own pixel geometry and all held input; stop never waits for capture or an action loop."

    def __init__(self) -> None:
        self.user = ct.WinDLL("user32", use_last_error=True)
        self._stopped = threading.Event()
        self._lock = threading.RLock()
        self._held: list[Input] = []
        self._frames: dict[tuple[Any, ...], tuple[int, ...]] = {}
        self._bind()

    def _bind(self) -> None:
        signatures = {
            "SendInput": ([ct.c_uint, ct.POINTER(Input), ct.c_int], ct.c_uint),
            "SetThreadDpiAwarenessContext": ([ct.c_void_p], ct.c_void_p),
            "GetWindowRect": ([ct.c_void_p, ct.POINTER(Rect)], ct.c_int),
            "GetWindowThreadProcessId": ([ct.c_void_p, ct.POINTER(ct.c_uint32)], ct.c_uint32),
            "GetForegroundWindow": ([], ct.c_void_p),
            "SetForegroundWindow": ([ct.c_void_p], ct.c_int),
            "IsIconic": ([ct.c_void_p], ct.c_int),
            "ShowWindow": ([ct.c_void_p, ct.c_int], ct.c_int),
            "GetKeyboardLayout": ([ct.c_uint32], ct.c_void_p),
            "VkKeyScanExW": ([ct.c_wchar, ct.c_void_p], ct.c_int16),
            "GetAsyncKeyState": ([ct.c_int], ct.c_int16),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = arguments, result

    @contextmanager
    def _physical(self):
        # Thread-local awareness avoids changing the hosting application's DPI policy.
        previous = self.user.SetThreadDpiAwarenessContext(ct.c_void_p(-4))
        try:
            yield
        finally:
            if previous:
                self.user.SetThreadDpiAwarenessContext(previous)

    def _check(self) -> None:
        if self._stopped.is_set():
            raise ComputerUseError(
                "Computer control is stopped. Wait for the user to resume it.",
                "computer_use_stopped",
            )

    def _send_raw(self, events: list[Input]) -> None:
        batch = (Input * len(events))(*events)
        if self.user.SendInput(len(batch), batch, ct.sizeof(Input)) != len(batch):
            raise ComputerUseError(
                (
                    "Windows refused input. Capture the desktop and check the active window"
                    " before continuing."
                ),
                "input_refused",
            )

    def _send(self, events: list[Input]) -> None:
        with self._lock:
            self._check()
            self._send_raw(events)

    def _press(self, down: Input, up: Input) -> None:
        with self._lock:
            self._check()
            self._held.append(up)
            self._send_raw([down])

    def release(self) -> None:
        with self._lock:
            if self._held:
                # Retain releases on failure so stop/close can try cleanup again.
                self._send_raw(list(reversed(self._held)))
                self._held.clear()

    def interrupt(self) -> None:
        self._stopped.set()
        self.release()

    def monitors(self) -> list[dict[str, Any]]:
        class MonitorInfo(ct.Structure):
            _fields_ = [
                ("size", ct.c_uint32),
                ("monitor", Rect),
                ("work", Rect),
                ("flags", ct.c_uint32),
                ("device", ct.c_wchar * 32),
            ]

        result: list[dict[str, Any]] = []
        callback_type = ct.WINFUNCTYPE(
            ct.c_int, ct.c_void_p, ct.c_void_p, ct.POINTER(Rect), ct.c_ssize_t
        )
        self.user.GetMonitorInfoW.argtypes = [ct.c_void_p, ct.POINTER(MonitorInfo)]
        self.user.EnumDisplayMonitors.argtypes = [
            ct.c_void_p,
            ct.c_void_p,
            callback_type,
            ct.c_ssize_t,
        ]

        def visit(handle, dc, rectangle, data):
            info = MonitorInfo(size=ct.sizeof(MonitorInfo))
            if not self.user.GetMonitorInfoW(handle, ct.byref(info)):
                return 0
            box = info.monitor
            result.append(
                {
                    "id": len(result) + 1,
                    "name": info.device,
                    "primary": bool(info.flags & 1),
                    "x": box.left,
                    "y": box.top,
                    "width": box.right - box.left,
                    "height": box.bottom - box.top,
                }
            )
            return 1

        with self._physical():
            if not self.user.EnumDisplayMonitors(None, None, callback_type(visit), 0) or not result:
                raise ComputerUseError(
                    "Windows has no accessible interactive display.", "display_unavailable"
                )
        return result

    def _geometry(self, args: dict[str, Any]) -> tuple[tuple[int, int, int, int], tuple[int, ...]]:
        monitors = self.monitors()
        layout = tuple(
            number
            for item in monitors
            for number in (item["x"], item["y"], item["width"], item["height"])
        )
        if "window_id" in args:
            pid = ct.c_uint32()
            self.user.GetWindowThreadProcessId(args["window_id"], ct.byref(pid))
            rectangle = Rect()
            if pid.value != args["pid"] or not self.user.GetWindowRect(
                args["window_id"], ct.byref(rectangle)
            ):
                raise ComputerUseError(
                    "The window is no longer available. Discover windows again.", "stale_window"
                )
            bounds = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
        elif "monitor" in args:
            selected = next((item for item in monitors if item["id"] == args["monitor"]), None)
            if selected is None:
                raise ComputerUseError(
                    "This display is unavailable. Call monitors again.", "display_unavailable"
                )
            bounds = (
                selected["x"],
                selected["y"],
                selected["x"] + selected["width"],
                selected["y"] + selected["height"],
            )
        else:
            bounds = (
                min(m["x"] for m in monitors),
                min(m["y"] for m in monitors),
                max(m["x"] + m["width"] for m in monitors),
                max(m["y"] + m["height"] for m in monitors),
            )
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise ComputerUseError(
                "The target has no visible image. Capture the desktop.", "display_unavailable"
            )
        return bounds, (*bounds, *layout)

    @staticmethod
    def _target(args: dict[str, Any]) -> tuple[Any, ...]:
        return (args.get("session"), args.get("pid"), args.get("window_id"), args.get("monitor"))

    def capture(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check()
        with self._physical():
            bounds, geometry = self._geometry(args)
            if (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) > 40_000_000:
                raise ComputerUseError(
                    "The desktop image is too large. Select one monitor.", "image_too_large"
                )
            image = ImageGrab.grab(bbox=bounds, all_screens=True, include_layered_windows=True)
            _, after = self._geometry(args)
        self._check()
        if geometry != after:
            raise ComputerUseError(
                "The display layout changed during capture. Capture again.", "stale_view"
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", compress_level=1)
        self._frames[self._target(args)] = geometry
        return {
            "screenshot_png_b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "screen_origin": list(bounds[:2]),
            "capture_backend": "windows",
            "coordinate_space": "image_pixels",
        }

    def _wait(self, seconds: float) -> None:
        self._stopped.wait(seconds)
        self._check()

    def _move(self, x: int, y: int) -> None:
        # Absolute input addresses the virtual desktop, including negative origins.
        bounds, geometry = self._geometry({})
        layout = geometry[4:]
        if not any(
            layout[i] <= x < layout[i] + layout[i + 2]
            and layout[i + 1] <= y < layout[i + 1] + layout[i + 3]
            for i in range(0, len(layout), 4)
        ):
            raise ComputerUseError(
                "The pointer target is outside the desktop.", "invalid_coordinates"
            )
        dx = round((x - bounds[0]) * 65535 / max(1, bounds[2] - bounds[0] - 1))
        dy = round((y - bounds[1]) * 65535 / max(1, bounds[3] - bounds[1] - 1))
        self._send([mouse(0xC001, x=dx, y=dy)])

    def _keys(self, names: list[str]) -> list[int]:
        thread = self.user.GetWindowThreadProcessId(self.user.GetForegroundWindow(), None)
        layout = self.user.GetKeyboardLayout(thread)
        result = []
        for name in names:
            if name in KEYS:
                result.append(KEYS[name])
                continue
            char = {"plus": "+", "minus": "-"}.get(name, name)
            key = self.user.VkKeyScanExW(char, layout) if len(char) == 1 else -1
            if key == -1:
                raise ComputerUseError(
                    "Unsupported key name. Use a named key or type for text.", "invalid_arguments"
                )
            result.extend(vk for mask, vk in ((1, 0x10), (2, 0x11), (4, 0x12)) if (key >> 8) & mask)
            result.append(key & 0xFF)
        return list(dict.fromkeys(result))

    def _text(self, text: str) -> None:
        for char in text.replace("\r\n", "\n"):
            if char in "\n\r\t":
                vk = 9 if char == "\t" else 13
                self._send([keyboard(vk), keyboard(vk, up=True)])
            else:
                raw = char.encode("utf-16-le")
                events = []
                for index in range(0, len(raw), 2):
                    scan = int.from_bytes(raw[index : index + 2], "little")
                    events.extend([keyboard(0, scan=scan), keyboard(0, scan=scan, up=True)])
                self._send(events)

    def input(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self._check()
        with self._physical():
            bounds, geometry = self._geometry(args)
            previous = self._frames.get(self._target(args))
            semantic_focus = (
                previous is None
                and "window_id" in args
                and name in {"type_text", "press_key", "hotkey", "scroll"}
                and "x" not in args
            )
            if previous != geometry and not semantic_focus:
                raise ComputerUseError(
                    "Capture the target again before sending input.", "capture_required"
                )
            if "window_id" in args:
                hwnd = args["window_id"]
                if self.user.IsIconic(hwnd):
                    raise ComputerUseError(
                        "The window is minimized. Restore it through the desktop first.",
                        "window_not_visible",
                    )
                if self.user.GetForegroundWindow() != hwnd:
                    self.user.SetForegroundWindow(hwnd)
                if self.user.GetForegroundWindow() != hwnd:
                    raise ComputerUseError(
                        "Windows did not activate the target. Select it through the desktop first.",
                        "focus_refused",
                    )
            try:
                if "x" in args:
                    self._move(bounds[0] + args["x"], bounds[1] + args["y"])
                if name == "move_cursor":
                    pass
                elif name == "click":
                    down, up = BUTTONS[args.get("button", "left")]
                    for index in range(args.get("count", 1)):
                        if index:
                            self._wait(0.05)
                        self._press(mouse(down), mouse(up))
                        self.release()
                elif name == "type_text":
                    self._text(args["text"])
                elif name in {"press_key", "hotkey"}:
                    keys = self._keys(args.get("keys", [args.get("key", "")]))
                    if any(self.user.GetAsyncKeyState(key) & 0x8000 for key in keys):
                        raise ComputerUseError(
                            "A requested key is already held. Release it before continuing.",
                            "input_busy",
                        )
                    for key in keys:
                        self._press(keyboard(key), keyboard(key, up=True))
                    self._wait(args.get("duration_ms", 0) / 1000)
                elif name == "scroll":
                    direction = args["direction"]
                    delta = (
                        120 * args.get("amount", 3) * (1 if direction in {"up", "right"} else -1)
                    )
                    self._send(
                        [mouse(0x1000 if direction in {"left", "right"} else 0x800, data=delta)]
                    )
                elif name == "drag":
                    start = (bounds[0] + args["from_x"], bounds[1] + args["from_y"])
                    end = (bounds[0] + args["to_x"], bounds[1] + args["to_y"])
                    self._move(*start)
                    down, up = BUTTONS[args.get("button", "left")]
                    self._press(mouse(down), mouse(up))
                    duration = args.get("duration_ms", 250) / 1000
                    count = max(1, round(duration / 0.016))
                    deadline = time.monotonic()
                    for i in range(1, count + 1):
                        self._wait(max(0, deadline + duration * i / count - time.monotonic()))
                        self._move(
                            round(start[0] + (end[0] - start[0]) * i / count),
                            round(start[1] + (end[1] - start[1]) * i / count),
                        )
                else:
                    raise ComputerUseError(
                        "This input is unavailable on the desktop.", "unsupported_action"
                    )
            finally:
                self.release()
            # Give the foreground message loop time to translate queued key events before
            # a subsequent shortcut changes its keyboard state (notably select-all/type).
            self._wait(0.025)
        return {
            "effect": "dispatched",
            "delivery": {"mode": "foreground"},
            "route": "windows_sendinput",
        }

    def end_session(self, session: str) -> None:
        self._frames = {key: value for key, value in self._frames.items() if key[0] != session}
