"""Put a transcript where the user's text cursor is (Windows).

The text goes through the clipboard: it is published as clipboard text and
pasted with a synthetic Ctrl+V into the window that had the focus when the
recording began. The paste is skipped, and the text simply stays in the
clipboard, when

- another window has the focus by now (the user moved on; a paste could land
  anywhere),
- the target runs elevated while the Desktop does not (Windows silently drops
  input sent to it), or its elevation cannot be checked,
- the user still holds a modifier after a short wait (Ctrl+V would turn into
  another shortcut).

After a paste, the previous clipboard text comes back once the target had time
to read the clipboard, unless something else wrote to the clipboard meanwhile.
Clipboard content other than text is not restored.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from desktop._windows import win32_library
from desktop.system_actions import read_windows_clipboard, write_windows_clipboard

logger = logging.getLogger("vbot.desktop.dictation.insertion")

INSERT_PASTED = "pasted"
INSERT_CLIPBOARD = "clipboard"
INSERT_FAILED = "failed"

# How long a modifier may still be held (the shortcut that ended the
# recording) before the paste gives up and leaves the text in the clipboard.
MODIFIER_RELEASE_TIMEOUT_SECONDS = 2.0
_MODIFIER_POLL_SECONDS = 0.02
# How long the target gets to read the pasted text before the previous
# clipboard text returns.
CLIPBOARD_RESTORE_DELAY_SECONDS = 0.8

_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_VK_V = 0x56
_MODIFIER_KEYS = (_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LWIN, _VK_RWIN)


class TextInserter(Protocol):
    """What Desktop dictation needs from the host to place its text."""

    def foreground_window(self) -> int:
        """Return the window that has the focus now (0 when none)."""

    def insert(self, text: str, target_window: int) -> str:
        """Place ``text`` in ``target_window``; returns an ``INSERT_*`` outcome."""


class WindowsHost(Protocol):
    """The Win32 calls :class:`ClipboardTextInserter` makes (a seam for tests)."""

    def foreground_window(self) -> int: ...

    def can_send_input_to(self, window: int) -> bool: ...

    def modifier_held(self) -> bool: ...

    def send_paste(self) -> bool: ...

    def clipboard_sequence(self) -> int: ...

    def read_clipboard_text(self) -> str: ...

    def write_clipboard_text(self, text: str) -> None: ...


class ClipboardTextInserter:
    """:class:`TextInserter` that pastes through the clipboard (see the module doc)."""

    def __init__(
        self,
        host: WindowsHost | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        schedule: Callable[[float, Callable[[], None]], None] | None = None,
    ) -> None:
        self._host: WindowsHost = host or _Win32Host()
        self._clock = clock
        self._sleep = sleep
        self._schedule = schedule or _schedule_on_timer

    def foreground_window(self) -> int:
        try:
            return self._host.foreground_window()
        except Exception:
            logger.warning("The focused window could not be read", exc_info=True)
            return 0

    def insert(self, text: str, target_window: int) -> str:
        try:
            previous = self._host.read_clipboard_text()
        except Exception:
            logger.info("The clipboard text could not be read; it will not be restored")
            previous = ""
        try:
            self._host.write_clipboard_text(text)
            written = self._host.clipboard_sequence()
        except Exception:
            logger.warning("Dictation could not write the clipboard", exc_info=True)
            return INSERT_FAILED
        if not self._may_paste(target_window):
            return INSERT_CLIPBOARD
        try:
            pasted = self._host.send_paste()
        except Exception:
            logger.warning("Dictation could not send the paste", exc_info=True)
            pasted = False
        if not pasted:
            return INSERT_CLIPBOARD
        if previous:
            self._schedule(
                CLIPBOARD_RESTORE_DELAY_SECONDS, lambda: self._restore(previous, written)
            )
        return INSERT_PASTED

    def _may_paste(self, target_window: int) -> bool:
        if target_window == 0 or self.foreground_window() != target_window:
            logger.info("The focus moved during dictation; the text stays in the clipboard")
            return False
        try:
            reachable = self._host.can_send_input_to(target_window)
        except Exception:
            logger.warning("The target window's elevation could not be checked", exc_info=True)
            reachable = False
        if not reachable:
            logger.info("The target window runs elevated; the text stays in the clipboard")
            return False
        deadline = self._clock() + MODIFIER_RELEASE_TIMEOUT_SECONDS
        while self._host.modifier_held():
            if self._clock() >= deadline:
                logger.info("A modifier is still held; the text stays in the clipboard")
                return False
            self._sleep(_MODIFIER_POLL_SECONDS)
        return True

    def _restore(self, previous: str, written: int) -> None:
        try:
            if self._host.clipboard_sequence() != written:
                return  # the user or an app copied something new meanwhile
            self._host.write_clipboard_text(previous)
        except Exception:
            logger.warning("The previous clipboard text could not be restored", exc_info=True)


def _schedule_on_timer(delay: float, action: Callable[[], None]) -> None:
    timer = threading.Timer(delay, action)
    timer.name = "vbot-dictation-clipboard"
    timer.daemon = True
    timer.start()


class _Win32Host:
    """ctypes binding of :class:`WindowsHost`."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = win32_library("user32")
        kernel32 = win32_library("kernel32")
        advapi32 = win32_library("advapi32")

        self._get_foreground_window = user32.GetForegroundWindow
        self._get_foreground_window.argtypes = []
        self._get_foreground_window.restype = wintypes.HWND
        self._get_window_thread_process_id = user32.GetWindowThreadProcessId
        self._get_window_thread_process_id.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._get_window_thread_process_id.restype = wintypes.DWORD
        self._get_async_key_state = user32.GetAsyncKeyState
        self._get_async_key_state.argtypes = [ctypes.c_int]
        self._get_async_key_state.restype = ctypes.c_short
        self._get_clipboard_sequence_number = user32.GetClipboardSequenceNumber
        self._get_clipboard_sequence_number.argtypes = []
        self._get_clipboard_sequence_number.restype = wintypes.DWORD
        self._map_virtual_key = user32.MapVirtualKeyW
        self._map_virtual_key.argtypes = [wintypes.UINT, wintypes.UINT]
        self._map_virtual_key.restype = wintypes.UINT
        self._send_input = user32.SendInput
        self._send_input.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
        self._send_input.restype = wintypes.UINT

        self._open_process = kernel32.OpenProcess
        self._open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._open_process.restype = wintypes.HANDLE
        self._get_current_process = kernel32.GetCurrentProcess
        self._get_current_process.argtypes = []
        self._get_current_process.restype = wintypes.HANDLE
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL
        self._open_process_token = advapi32.OpenProcessToken
        self._open_process_token.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self._open_process_token.restype = wintypes.BOOL
        self._get_token_information = advapi32.GetTokenInformation
        self._get_token_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._get_token_information.restype = wintypes.BOOL
        self._input_type = _keyboard_input_type(ctypes, wintypes)
        self._self_elevated: bool | None = None

    def foreground_window(self) -> int:
        return int(self._get_foreground_window() or 0)

    def can_send_input_to(self, window: int) -> bool:
        process_id = self._wintypes.DWORD()
        self._get_window_thread_process_id(window, self._ctypes.byref(process_id))
        if not process_id.value:
            return False
        if self._self_elevated is None:
            self._self_elevated = self._elevated(self._get_current_process(), close=False)
        if self._self_elevated:
            return True
        process_query_limited_information = 0x1000
        process = self._open_process(process_query_limited_information, False, process_id.value)
        if not process:
            return False  # protected or elevated beyond our reach: assume blocked
        target_elevated = self._elevated(process, close=True)
        return target_elevated is False

    def _elevated(self, process: Any, *, close: bool) -> bool | None:
        token_query = 0x0008
        token_elevation = 20
        token = self._wintypes.HANDLE()
        try:
            if not self._open_process_token(process, token_query, self._ctypes.byref(token)):
                return None
            try:
                elevation = self._wintypes.DWORD()
                size = self._wintypes.DWORD()
                if not self._get_token_information(
                    token,
                    token_elevation,
                    self._ctypes.byref(elevation),
                    self._ctypes.sizeof(elevation),
                    self._ctypes.byref(size),
                ):
                    return None
                return bool(elevation.value)
            finally:
                self._close_handle(token)
        finally:
            if close:
                self._close_handle(process)

    def modifier_held(self) -> bool:
        return any(self._get_async_key_state(key) & 0x8000 for key in _MODIFIER_KEYS)

    def send_paste(self) -> bool:
        keyeventf_keyup = 0x0002
        strokes = [
            (_VK_CONTROL, 0),
            (_VK_V, 0),
            (_VK_V, keyeventf_keyup),
            (_VK_CONTROL, keyeventf_keyup),
        ]
        mapvk_vk_to_vsc = 0
        inputs = (self._input_type * len(strokes))()
        for item, (virtual_key, flags) in zip(inputs, strokes, strict=True):
            item.type = 1  # INPUT_KEYBOARD
            item.ki.wVk = virtual_key
            item.ki.wScan = self._map_virtual_key(virtual_key, mapvk_vk_to_vsc)
            item.ki.dwFlags = flags
        sent = self._send_input(len(strokes), inputs, self._ctypes.sizeof(self._input_type))
        return int(sent) == len(strokes)

    def clipboard_sequence(self) -> int:
        return int(self._get_clipboard_sequence_number())

    def read_clipboard_text(self) -> str:
        return read_windows_clipboard()

    def write_clipboard_text(self, text: str) -> None:
        write_windows_clipboard(text)


def _keyboard_input_type(ctypes: Any, wintypes: Any) -> Any:
    """Return the ``INPUT`` structure, sized by its largest union member."""

    class KeyboardInput(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        )

    class MouseInput(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        )

    class InputUnion(ctypes.Union):
        _fields_ = (("ki", KeyboardInput), ("mi", MouseInput))

    class Input(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = (("type", wintypes.DWORD), ("union", InputUnion))

    return Input
