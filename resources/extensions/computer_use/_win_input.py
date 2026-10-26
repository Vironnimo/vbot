"""Foreground keyboard and mouse input of the Windows desktop target through ``SendInput``.

``WindowsInput`` implements the input half of ``DesktopTarget``. It remembers
every key and button it holds down, checks the stop event between events, and
releases everything it holds before an interruption or input failure surfaces.
Typed text goes as the keys of the foreground window's keyboard layout, like a
real keyboard, so apps that read raw keys receive it; characters the layout has
no plain key for go as Unicode input.
"""

from __future__ import annotations

import ctypes as ct
import itertools
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Protocol

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
_VK_CAPITAL = 0x14
# Asks an input method window whether it composes typed keys (IMC_GETOPENSTATUS).
_WM_IME_CONTROL, _IMC_GETOPENSTATUS = 0x283, 0x5
_SMTO_ABORTIFHUNG, _IME_QUERY_MILLISECONDS = 0x2, 200
_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYUP, _EXTENDED, _UNICODE = 0x2, 0x1, 0x4
_MOVE, _ABSOLUTE_VIRTUAL, _WHEEL, _HWHEEL = 0x1, 0x8000 | 0x4000, 0x800, 0x1000
_BUTTON_FLAGS = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}
_WHEEL_DELTA = 120
_VK_F24 = 0x87
# Windows 11 Notepad and other WinUI text boxes drop or repeat keys that go down and
# up in the same instant; a held key followed by a short gap reads like real typing.
_KEY_HOLD, _KEY_GAP = 0.02, 0.02
# A drag moves the pointer this often, so apps that sample it see a continuous stroke.
_DRAG_INTERVAL = 0.01
_DRAG_MAX_STEPS = 10_000
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


def drag_steps(path: list[tuple[int, int]], seconds: float) -> list[tuple[float, int, int]]:
    """Plan a drag through *path*: ``(time, x, y)`` moves after the button went down.

    The pointer travels the straight segments between the points at constant speed
    and passes every point exactly; a move comes about every ``_DRAG_INTERVAL``.
    Without distance (all points equal) the time is split evenly between segments.
    """
    segments = list(itertools.pairwise(path))
    lengths = [math.dist(start, end) for start, end in segments]
    total = sum(lengths)
    wanted = min(_DRAG_MAX_STEPS, max(len(segments), math.ceil(seconds / _DRAG_INTERVAL)))
    steps: list[tuple[float, int, int]] = []
    elapsed = 0.0
    for (start, end), length in zip(segments, lengths, strict=True):
        share = length / total if total else 1 / len(segments)
        count = max(1, math.ceil(wanted * share))
        for step in range(1, count + 1):
            fraction = step / count
            steps.append(
                (
                    elapsed + seconds * share * fraction,
                    round(start[0] + (end[0] - start[0]) * fraction),
                    round(start[1] + (end[1] - start[1]) * fraction),
                )
            )
        elapsed += seconds * share
    return steps


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


class KeyboardLayout(Protocol):
    """What the keys of a keyboard layout type; probing never changes its state."""

    def scan(self, char: str) -> int:
        """``VkKeyScanExW``: the virtual key and shift state that type *char*, or -1."""
        ...

    def output(self, vk: int, modifiers: tuple[str, ...]) -> str | None:
        """What pressing *vk* with *modifiers* types now; ``None`` for a dead key."""
        ...


@dataclass(frozen=True)
class Stroke:
    """One key press that types a character, with the modifiers held for it."""

    vk: int
    modifiers: tuple[str, ...] = ()


def layout_stroke(char: str, layout: KeyboardLayout) -> Stroke | None:
    """The key press that types exactly *char* on *layout*; ``None`` when no plain one does.

    Control characters never qualify: their keys are shortcuts (U+0003 is ctrl+c).
    A dead key would only start an accent that changes the next character. The
    press must type *char* itself; CapsLock inverts Shift for letters, so the other
    Shift state is tried as well.
    """
    if len(char) != 1 or char > "\uffff" or not char.isprintable():
        return None
    decoded = decode_key_scan(layout.scan(char))
    if decoded is None:
        return None
    vk, modifiers = decoded
    # The same modifiers with Shift toggled.
    toggled = tuple(name for _, name in _SHIFT_STATE if (name in modifiers) != (name == "shift"))
    for candidate in (modifiers, toggled):
        typed = layout.output(vk, candidate)
        if typed is None:
            return None
        if typed == char:
            return Stroke(vk, candidate)
    return None


def text_units(
    text: str, layout: KeyboardLayout | None = None
) -> list[str | Stroke | tuple[int, ...]]:
    """Plan typed text: Enter or Tab key names, a layout key press for each character
    *layout* types with one, else the character's UTF-16 code units for Unicode input.

    Windows line endings and lone carriage returns each press Enter once. Without a
    layout every character goes as Unicode input.
    """
    plan: list[str | Stroke | tuple[int, ...]] = []
    for char in text.replace("\r\n", "\n").replace("\r", "\n"):
        if char == "\n":
            plan.append("enter")
            continue
        if char == "\t":
            plan.append("tab")
            continue
        stroke = layout_stroke(char, layout) if layout is not None else None
        if stroke is not None:
            plan.append(stroke)
            continue
        encoded = char.encode("utf-16-le")
        plan.append(
            tuple(int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2))
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
            self._release_keys(keys, strict=True)
        finally:
            self._release(f"button:{button}")
            self._release_keys(keys)

    def drag(self, path: list[tuple[int, int]], seconds: float, modifiers: list[str]) -> None:
        _win32.enter_thread()
        keys = self._plan(modifiers)
        for point in path:
            self._inside_desktop(*point)
        press, release = self._button_flags("left")
        self._move(*path[0])
        try:
            self._press_keys(keys)
            self._press("button:left", mouse_event(press), mouse_event(release))
            self._pause(0.05)
            began = time.monotonic()
            for at, x, y in drag_steps(path, seconds):
                self._pause(at - (time.monotonic() - began))
                self._move(x, y)
            self._pause(0.05)  # let the drop target notice the hover before release
            self._release("button:left", strict=True)
            self._release_keys(keys, strict=True)
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
            self._release_keys(keys, strict=True)
        finally:
            self._release_keys(keys)

    def keys(self, chord: list[str], repeat: int) -> None:
        _win32.enter_thread()
        keys = self._plan(chord)
        for index in range(repeat):
            if index:
                self._pause(0.03)
            self._tap(keys)

    def hold(self, chord: list[str], seconds: float) -> None:
        _win32.enter_thread()
        keys = self._plan(chord)
        try:
            self._press_keys(keys)
            self._pause(seconds)
            self._release_keys(keys, strict=True)
        finally:
            self._release_keys(keys)

    def type_text(self, text: str) -> None:
        _win32.enter_thread()
        window = int(_win32.api().user32.GetForegroundWindow() or 0)
        layout = _foreground_layout(window)
        # An open input method (Japanese, Chinese, ...) would compose layout keys into
        # other characters; Unicode input passes it by.
        probe = None if _input_method_open(window) else _LayoutProbe(layout, _caps_lock_on())
        for unit in text_units(text, probe):
            if isinstance(unit, str):
                self.keys([unit], 1)
                continue
            if isinstance(unit, Stroke):
                ordered = [name for name in MODIFIER_KEYS if name in unit.modifiers]
                self._tap([*(self._named(name, layout) for name in ordered), _key(unit.vk, layout)])
                continue
            names = [f"text:{index}" for index in range(len(unit))]
            try:
                for name, code in zip(names, unit, strict=True):
                    down = key_event(0, code, _UNICODE)
                    self._press(name, down, key_event(0, code, _UNICODE | _KEYUP))
                self._pause(_KEY_HOLD)
                for name in reversed(names):
                    self._release(name, strict=True)
            finally:
                for name in reversed(names):
                    self._release(name)
            self._pause(_KEY_GAP)

    def _nudge(self) -> None:
        """A zero mouse move: makes this process the source of the latest input."""
        self._send([mouse_event(_MOVE)])

    def _tap_unbound_key(self) -> None:
        """Tap F24, a key no app binds: makes this process the source of the latest input.

        Not Alt: an Alt tap puts the window brought to the front into menu mode (Paint
        shows its key tips), which swallows the next click and turns letters into
        shortcuts.
        """
        self._tap([_Key(_VK_F24, 0, False)])

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
            up = self._held.get(name)
            if up is None:
                return
            if self._send_events([up]) == 1:
                del self._held[name]
                return
            if strict:
                self.release_all()
                raise TargetError(_BLOCKED, "computer_use_unavailable")

    def _tap(self, keys: list[_Key]) -> None:
        """Press *keys* in order, hold them, release them in reverse order, then pause."""
        try:
            self._press_keys(keys)
            self._pause(_KEY_HOLD)
            self._release_keys(keys, strict=True)
        finally:
            self._release_keys(keys)
        self._pause(_KEY_GAP)

    def _press_keys(self, keys: list[_Key]) -> None:
        for key in keys:
            self._press(f"key:{key.vk}", key.event(False), key.event(True))

    def _release_keys(self, keys: list[_Key], *, strict: bool = False) -> None:
        for key in reversed(keys):
            self._release(f"key:{key.vk}", strict=strict)

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
        probe = _LayoutProbe(layout)
        decoded = decode_key_scan(probe.scan(char)) if char <= "\uffff" else None
        if decoded is None:
            raise TargetError(
                f'The active keyboard layout has no key for "{char}". Use type to enter it.',
                "invalid_arguments",
            )
        vk, modifiers = decoded
        if probe.output(vk, modifiers) is None:
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


class _LayoutProbe:
    """A Windows keyboard layout as ``KeyboardLayout``, with the current CapsLock state."""

    def __init__(self, layout: int, caps_lock: bool = False) -> None:
        self._layout, self._caps_lock = layout, caps_lock

    def scan(self, char: str) -> int:
        return int(_win32.api().user32.VkKeyScanExW(char, self._layout))

    def output(self, vk: int, modifiers: tuple[str, ...]) -> str | None:
        user32 = _win32.api().user32
        state = (ct.c_ubyte * 256)()
        for modifier in modifiers:
            state[NAMED_KEYS[modifier]] = 0x80
        if self._caps_lock:
            state[_VK_CAPITAL] = 0x01
        output = ct.create_unicode_buffer(8)
        scan = user32.MapVirtualKeyExW(vk, 0, self._layout)
        # Flag 4 probes without changing the layout's pending dead-key state.
        count = int(user32.ToUnicodeEx(vk, scan, state, output, len(output), 4, self._layout))
        return None if count < 0 else output.value[:count]


def _foreground_layout(window: int | None = None) -> int:
    """The keyboard layout of *window*'s thread, by default the foreground window's."""
    user32 = _win32.api().user32
    if window is None:
        window = user32.GetForegroundWindow()
    thread = int(user32.GetWindowThreadProcessId(window, None))
    return int(user32.GetKeyboardLayout(thread) or 0)


def _caps_lock_on() -> bool:
    return bool(_win32.api().user32.GetKeyState(_VK_CAPITAL) & 1)


def _input_method_open(window: int) -> bool:
    """Whether an input method composes the keys typed into *window*.

    ``False`` when the window has no input method window or does not answer quickly.
    """
    bound = _win32.api()
    ime = bound.imm32.ImmGetDefaultIMEWnd(window) if window else None
    status = ct.c_size_t()
    if not ime or not bound.user32.SendMessageTimeoutW(
        ime,
        _WM_IME_CONTROL,
        _IMC_GETOPENSTATUS,
        0,
        _SMTO_ABORTIFHUNG,
        _IME_QUERY_MILLISECONDS,
        ct.byref(status),
    ):
        return False
    return bool(status.value)


def _double_click_seconds() -> float:
    return int(_win32.api().user32.GetDoubleClickTime()) / 1000
