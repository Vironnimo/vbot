"""Foreground keyboard and mouse input of the Windows desktop target through ``SendInput``.

``WindowsInput`` implements the input half of ``DesktopTarget``. It remembers
every key and button it holds down, checks the stop event between events, and
releases everything it holds before an interruption or input failure surfaces.
"""

from __future__ import annotations

import ctypes as ct
import logging
import threading

from . import _win32
from .target import BUTTONS, InputInterrupted, TargetError

_LOGGER = logging.getLogger("vbot.extensions.computer_use")


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


class _InputData(ct.Union):
    _fields_ = [("mouse", MouseInput), ("key", KeyInput)]


class Input(ct.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", ct.c_uint32), ("value", _InputData)]


# Virtual keys of the canonical key names (``_keys``); characters use the layout.
NAMED_KEYS: dict[str, int] = {
    "ctrl": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "enter": 0x0D,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "space": 0x20,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "printscreen": 0x2C,
    "pause": 0x13,
    "menu": 0x5D,
    **{f"f{number}": 0x6F + number for number in range(1, 25)},
    "volumeup": 0xAF,
    "volumedown": 0xAE,
    "volumemute": 0xAD,
    "medianext": 0xB0,
    "mediaprev": 0xB1,
    "mediaplaypause": 0xB3,
    "browserback": 0xA6,
    "browserforward": 0xA7,
}
MODIFIER_KEYS = ("ctrl", "shift", "alt", "win")
# Keys Windows treats as extended even when the layout maps no E0 scan code for them.
EXTENDED_KEYS = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5D, 0x90}
    | set(range(0xA6, 0xB8))
)
_SHIFT_STATE = ((1, "shift"), (2, "ctrl"), (4, "alt"))  # VkKeyScanExW shift-state bits
_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYUP, _EXTENDED, _UNICODE = 0x2, 0x1, 0x4
_MOVE, _ABSOLUTE_VIRTUAL, _WHEEL, _HWHEEL = 0x1, 0x8000 | 0x4000, 0x800, 0x1000
_BUTTON_FLAGS = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}
_WHEEL_DELTA = 120
_TEXT_CHUNK = 20  # characters per SendInput call; stop is checked between chunks
_DRAG_STEPS, _DRAG_SECONDS = 15, 0.25
_BLOCKED = (
    "Windows blocked the input: the screen may be locked, or a secure prompt such as "
    "User Account Control may be open. Take a screenshot to check, and ask the user to "
    "unlock the screen or answer the prompt."
)


def decode_key_scan(result: int) -> tuple[int, tuple[str, ...]] | None:
    """Split a ``VkKeyScanExW`` result into a virtual key and the modifiers it needs.

    ``None`` when the layout has no key for the character or the character needs a
    shift state a chord cannot express (Kana, OEM-specific states).
    """
    if result == -1 or result & 0xFFFF == 0xFFFF:
        return None
    vk, state = result & 0xFF, (result >> 8) & 0xFF
    if state & ~0x7:
        return None
    return vk, tuple(name for bit, name in _SHIFT_STATE if state & bit)


def absolute_coordinate(position: int, origin: int, size: int) -> int:
    """Normalize a virtual-desktop pixel for ``MOUSEEVENTF_ABSOLUTE | VIRTUALDESK``.

    Windows maps a normalized value back with ``floor(value * size / 65536)``; one
    step above the exact multiple lands on *position* itself.
    """
    return (65536 * (position - origin)) // size + 1


def key_event(vk: int, scan: int, flags: int) -> Input:
    return Input(type=_INPUT_KEYBOARD, key=KeyInput(vk=vk, scan=scan, flags=flags))


def mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> Input:
    return Input(
        type=_INPUT_MOUSE, mouse=MouseInput(dx=dx, dy=dy, data=data & 0xFFFFFFFF, flags=flags)
    )


def text_units(text: str) -> list[str | int]:
    """Plan typed text: Enter or Tab key names, else UTF-16 code units.

    Windows line endings and lone carriage returns each press Enter once.
    """
    plan: list[str | int] = []
    for char in text.replace("\r\n", "\n").replace("\r", "\n"):
        if char == "\n":
            plan.append("enter")
        elif char == "\t":
            plan.append("tab")
        else:
            encoded = char.encode("utf-16-le")
            plan.extend(
                int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)
            )
    return plan


class _Key:
    __slots__ = ("extended", "scan", "vk")

    def __init__(self, vk: int, scan: int, extended: bool) -> None:
        self.vk, self.scan, self.extended = vk, scan, extended

    def event(self, up: bool) -> Input:
        flags = (_KEYUP if up else 0) | (_EXTENDED if self.extended else 0)
        return key_event(self.vk, self.scan, flags)


class WindowsInput:
    """The input half of the Windows ``DesktopTarget``; one instance per target."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.RLock()
        # Held keys and buttons in press order, each with the event that releases it.
        self._held: dict[str, Input] = {}

    @staticmethod
    def enter_thread() -> None:
        """Make the calling thread use physical pixels; every method also does this."""
        _win32.enter_thread()

    def set_stop_event(self, event: threading.Event) -> None:
        self._stop = event

    def release_all(self) -> None:
        with self._lock:
            if not self._held:
                return
            events = list(reversed(self._held.values()))
            if self._send_events(events) == len(events):
                self._held.clear()
            else:
                # Kept so the next release (stop, Run end, shutdown) tries again.
                _LOGGER.warning("Computer Use input release refused (count=%d)", len(events))

    def cursor(self) -> tuple[int, int]:
        _win32.enter_thread()
        point = _win32.Point()
        if not _win32.api().user32.GetCursorPos(ct.byref(point)):
            raise TargetError(_BLOCKED, "computer_use_unavailable")
        return int(point.x), int(point.y)

    def move(self, x: int, y: int) -> None:
        _win32.enter_thread()
        self._move(x, y)

    def button(self, button: str, down: bool) -> None:
        _win32.enter_thread()
        press, release = self._button_flags(button)
        name = f"button:{button}"
        if down:
            self._press(name, mouse_event(press), mouse_event(release))
        elif name in self._held:
            self._release(name, strict=True)
        else:
            self._send([mouse_event(release)])

    def click(self, x: int, y: int, button: str, count: int, modifiers: list[str]) -> None:
        _win32.enter_thread()
        keys = self._plan(modifiers)
        press, release = self._button_flags(button)
        interval = min(0.08, _double_click_seconds() / 4)
        self._move(x, y)
        try:
            self._press_keys(keys)
            for index in range(count):
                if index:
                    self._pause(interval)
                self._press(f"button:{button}", mouse_event(press), mouse_event(release))
                self._pause(0.01)
                self._release(f"button:{button}", strict=True)
        finally:
            self._release_keys(keys)

    def drag(self, start: tuple[int, int], end: tuple[int, int], modifiers: list[str]) -> None:
        _win32.enter_thread()
        keys = self._plan(modifiers)
        self._inside_desktop(*end)
        press, release = self._button_flags("left")
        self._move(*start)
        try:
            self._press_keys(keys)
            self._press("button:left", mouse_event(press), mouse_event(release))
            self._pause(0.05)
            for step in range(1, _DRAG_STEPS + 1):
                self._pause(_DRAG_SECONDS / _DRAG_STEPS)
                self._move(
                    round(start[0] + (end[0] - start[0]) * step / _DRAG_STEPS),
                    round(start[1] + (end[1] - start[1]) * step / _DRAG_STEPS),
                )
            self._pause(0.05)  # let the drop target notice the hover before release
            self._release("button:left", strict=True)
        finally:
            self._release("button:left")
            self._release_keys(keys)

    def scroll(self, x: int, y: int, direction: str, amount: int, modifiers: list[str]) -> None:
        _win32.enter_thread()
        keys = self._plan(modifiers)
        flags = _HWHEEL if direction in ("left", "right") else _WHEEL
        delta = _WHEEL_DELTA if direction in ("up", "right") else -_WHEEL_DELTA
        self._move(x, y)
        try:
            self._press_keys(keys)
            for tick in range(amount):
                if tick:
                    self._pause(0.02)
                self._send([mouse_event(flags, data=delta)])
        finally:
            self._release_keys(keys)

    def keys(self, chord: list[str], repeat: int) -> None:
        _win32.enter_thread()
        keys = self._plan(chord)
        for index in range(repeat):
            if index:
                self._pause(0.03)
            try:
                self._press_keys(keys)
                self._pause(0.01)  # let raw-input readers see the chord before release
            finally:
                self._release_keys(keys)

    def hold(self, chord: list[str], seconds: float) -> None:
        _win32.enter_thread()
        keys = self._plan(chord)
        try:
            self._press_keys(keys)
            self._pause(seconds)
        finally:
            self._release_keys(keys)

    def type_text(self, text: str) -> None:
        _win32.enter_thread()
        plan = text_units(text)
        batch: list[Input] = []
        characters = 0
        for unit in plan:
            if isinstance(unit, str):
                self._send(batch)
                batch = []
                key = self._named(unit)
                self._send([key.event(False), key.event(True)])
                self._pause(0.01)
                continue
            batch += [key_event(0, unit, _UNICODE), key_event(0, unit, _UNICODE | _KEYUP)]
            characters += 1
            if characters % _TEXT_CHUNK == 0:
                self._send(batch)
                batch = []
                self._pause(0.005)
        self._send(batch)

    def _nudge(self) -> None:
        """A zero mouse move: makes this process the source of the latest input."""
        self._send([mouse_event(_MOVE)])

    def _tap_alt(self) -> None:
        key = self._named("alt")
        self._send([key.event(False), key.event(True)])

    # Mechanics

    def _check(self) -> None:
        if self._stop.is_set():
            self.release_all()
            raise InputInterrupted()

    def _pause(self, seconds: float) -> None:
        if (seconds > 0 and self._stop.wait(seconds)) or self._stop.is_set():
            self.release_all()
            raise InputInterrupted()

    def _send_events(self, events: list[Input]) -> int:
        batch = (Input * len(events))(*events)
        return int(_win32.api().user32.SendInput(len(batch), batch, ct.sizeof(Input)))

    def _send(self, events: list[Input]) -> None:
        if not events:
            return
        with self._lock:
            self._check()
            if self._send_events(events) != len(events):
                self.release_all()
                raise TargetError(_BLOCKED, "computer_use_unavailable")

    def _press(self, name: str, down: Input, up: Input) -> None:
        with self._lock:
            self._check()
            self._held[name] = up  # recorded first so a failed press is still released
            self._send([down])

    def _release(self, name: str, *, strict: bool = False) -> None:
        """Release one held key or button; never checks stop, releasing is always due."""
        with self._lock:
            up = self._held.pop(name, None)
            if up is None or self._send_events([up]) == 1:
                return
            self._held[name] = up
            if strict:
                raise TargetError(_BLOCKED, "computer_use_unavailable")

    def _press_keys(self, keys: list[_Key]) -> None:
        for key in keys:
            self._press(f"key:{key.vk}", key.event(False), key.event(True))

    def _release_keys(self, keys: list[_Key]) -> None:
        for key in reversed(keys):
            self._release(f"key:{key.vk}")

    def _plan(self, names: list[str]) -> list[_Key]:
        """Resolve a chord before any input: modifiers first, then the other keys."""
        layout = _foreground_layout()
        modifiers: list[str] = []
        others: list[_Key] = []
        for name in names:
            if name in MODIFIER_KEYS:
                modifiers.append(name)
            elif name in NAMED_KEYS or name == " ":
                others.append(self._named("space" if name == " " else name, layout))
            elif len(name) == 1:
                vk, implied = self._character(name, layout)
                modifiers.extend(implied)
                others.append(_key(vk, layout))
            else:
                raise TargetError(
                    f'"{name}" is not a key name. Use names such as enter, escape, tab, f5, '
                    "ctrl, shift, alt or win, or a single character; use type for text.",
                    "invalid_arguments",
                )
        ordered = [name for name in MODIFIER_KEYS if name in modifiers]
        keys = [self._named(name, layout) for name in ordered]
        for key in others:
            if all(key.vk != existing.vk for existing in keys):
                keys.append(key)
        return keys

    @staticmethod
    def _named(name: str, layout: int | None = None) -> _Key:
        return _key(NAMED_KEYS[name], _foreground_layout() if layout is None else layout)

    @staticmethod
    def _character(char: str, layout: int) -> tuple[int, tuple[str, ...]]:
        user32 = _win32.api().user32
        decoded = decode_key_scan(int(user32.VkKeyScanExW(char, layout))) if char <= "￿" else None
        if decoded is None:
            raise TargetError(
                f'The active keyboard layout has no key for "{char}". Use type to enter it.',
                "invalid_arguments",
            )
        vk, modifiers = decoded
        state = (ct.c_ubyte * 256)()
        for modifier in modifiers:
            state[NAMED_KEYS[modifier]] = 0x80
        output = ct.create_unicode_buffer(8)
        scan = user32.MapVirtualKeyExW(vk, 0, layout)
        # Flag 4 probes without changing the layout's pending dead-key state.
        if int(user32.ToUnicodeEx(vk, scan, state, output, len(output), 4, layout)) < 0:
            raise TargetError(
                f'"{char}" is a dead key on the active keyboard layout, so pressing it would '
                "only start an accent. Use type to enter it.",
                "invalid_arguments",
            )
        return vk, modifiers

    @staticmethod
    def _button_flags(button: str) -> tuple[int, int]:
        if button not in BUTTONS:
            raise TargetError(f'"{button}" is not a mouse button.', "invalid_arguments")
        # Windows applies a swapped-button setting to injected input too; the Agent
        # means the primary (left) or secondary (right) button.
        if button != "middle" and _win32.buttons_swapped():
            button = "right" if button == "left" else "left"
        return _BUTTON_FLAGS[button]

    @staticmethod
    def _inside_desktop(x: int, y: int) -> tuple[int, int, int, int]:
        left, top, width, height = _win32.virtual_screen()
        if not (left <= x < left + width and top <= y < top + height):
            raise TargetError("The point is outside the desktop.", "invalid_arguments")
        return left, top, width, height

    def _move(self, x: int, y: int) -> None:
        left, top, width, height = self._inside_desktop(x, y)
        dx, dy = absolute_coordinate(x, left, width), absolute_coordinate(y, top, height)
        self._send([mouse_event(_MOVE | _ABSOLUTE_VIRTUAL, dx, dy)])
        point = _win32.Point()
        user32 = _win32.api().user32
        if user32.GetCursorPos(ct.byref(point)) and (point.x, point.y) != (x, y):
            user32.SetCursorPos(x, y)  # a clipped or rounded move still ends exactly here


def _key(vk: int, layout: int) -> _Key:
    if vk == 0x13:  # Pause reports the last byte of its E1 sequence, not extended
        return _Key(vk, 0x45, False)
    code = int(_win32.api().user32.MapVirtualKeyExW(vk, 4, layout))  # MAPVK_VK_TO_VSC_EX
    # The scan code lets apps that read physical keys (browsers' KeyboardEvent.code) see it.
    return _Key(vk, code & 0xFF, code >> 8 == 0xE0 or vk in EXTENDED_KEYS)


def _foreground_layout() -> int:
    user32 = _win32.api().user32
    thread = int(user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None))
    return int(user32.GetKeyboardLayout(thread) or 0)


def _double_click_seconds() -> float:
    return int(_win32.api().user32.GetDoubleClickTime()) / 1000
