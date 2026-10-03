"""The emergency stop: two physical Esc presses end the active Computer Use call.

A low-level keyboard hook observes Esc globally on its own thread. Two physical
presses within 600 ms, with a key-up between them, request a stop; auto-repeat
and injected input (the Agent's own keystrokes) never count, and every event
passes through to the foreground app unchanged. The hotkey is armed only for
the active invocation: its owner token travels with the request to a separate
dispatch worker, so a callback that runs late cannot stop a later call, and a
slow callback never blocks the hook.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

_ESCAPE = 0x1B
_DOUBLE_PRESS_SECONDS = 0.6
_INJECTED = 0x12  # LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
_KEY_DOWN, _KEY_UP, _SYS_KEY_DOWN, _SYS_KEY_UP = 0x100, 0x101, 0x104, 0x105
_WM_QUIT = 0x0012
_WH_KEYBOARD_LL = 13


class EmergencyHotkey:
    """Observe physical double-Esc globally, dispatching stop off the hook thread."""

    def __init__(self, callback: Callable[[object], None]) -> None:
        self.callback = callback
        self.available = False
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._closing = threading.Event()
        self._requested = threading.Event()
        self._worker: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._owner: object | None = None
        self._requested_owner: object | None = None
        self._down = False
        self._first_press: float | None = None

    def set_armed(self, owner: object | None) -> None:
        """Arm for *owner* (the active invocation), or disarm with ``None``."""
        with self._state_lock:
            if owner is not self._owner:
                self._owner = owner
                self._first_press = None
                self._down = False

    @property
    def pending_owner(self) -> object | None:
        """The owner whose stop was requested and is not yet dispatched."""
        with self._state_lock:
            return self._requested_owner if not self._closing.is_set() else None

    def _key_event(self, key: int, down: bool, flags: int) -> None:
        if flags & _INJECTED:  # Agent input never counts.
            return
        with self._state_lock:
            if self._owner is None:
                return
            if key != _ESCAPE:
                if down:
                    self._first_press = None
                return
            if not down:
                self._down = False
                return
            if self._down:  # Holding Esc and its auto-repeat are one press.
                return
            self._down = True
            now = time.monotonic()
            if self._first_press is not None and now - self._first_press <= _DOUBLE_PRESS_SECONDS:
                self._first_press = None
                self._requested_owner = self._owner
                self._owner = None
                self._requested.set()
            else:
                self._first_press = now

    def _dispatch(self) -> None:
        while not self._closing.is_set():
            self._requested.wait()
            with self._state_lock:
                if self._closing.is_set():
                    return
                owner = self._requested_owner
            try:
                if owner is not None:
                    self.callback(owner)
            finally:
                with self._state_lock:
                    # A later call may have received its own double-Esc while the
                    # previous callback was draining. Do not lose it.
                    if self._requested_owner is owner:
                        self._requested_owner = None
                        self._requested.clear()

    def start(self) -> None:
        """Install the hook (Windows only); ``available`` tells whether it works."""
        if sys.platform != "win32":
            return
        if self._thread is not None:
            return
        self._worker = threading.Thread(
            target=self._dispatch, name="computer-use-hotkey-dispatch", daemon=True
        )
        self._worker.start()
        self._thread = threading.Thread(
            target=self._listen, name="computer-use-hotkey", daemon=True
        )
        self._thread.start()
        self._ready.wait(2)

    def _listen(self) -> None:
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class KeyboardEvent(ctypes.Structure):
            _fields_ = [
                ("key", wintypes.DWORD),
                ("scan", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t),
            ]

        hook_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            hook_proc,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user.SetWindowsHookExW.restype = wintypes.HHOOK
        user.CallNextHookEx.argtypes = [
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user.CallNextHookEx.restype = ctypes.c_ssize_t
        user.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        user.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._thread_id = kernel.GetCurrentThreadId()

        @hook_proc
        def observe(code: int, message: int, pointer: int) -> int:
            if code == 0 and message in {_KEY_DOWN, _KEY_UP, _SYS_KEY_DOWN, _SYS_KEY_UP}:
                event = ctypes.cast(pointer, ctypes.POINTER(KeyboardEvent)).contents
                self._key_event(event.key, message in {_KEY_DOWN, _SYS_KEY_DOWN}, event.flags)
            # Never swallow normal Esc or interfere with the foreground app.
            return int(user.CallNextHookEx(None, code, message, pointer))

        hook = user.SetWindowsHookExW(_WH_KEYBOARD_LL, observe, kernel.GetModuleHandleW(None), 0)
        message = wintypes.MSG()
        # Ensure PostThreadMessage can wake shutdown even before GetMessage starts.
        user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self.available = bool(hook)
        self._ready.set()
        if not self.available:
            return
        try:
            while (
                not self._closing.is_set()
                and user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0
            ):
                pass
        finally:
            user.UnhookWindowsHookEx(hook)
            self.available = False

    def close(self) -> None:
        """Disarm, remove the hook and end both threads."""
        self._closing.set()
        self.set_armed(None)
        self._requested.set()
        if sys.platform == "win32" and self._thread_id and self.available:
            import ctypes

            ctypes.WinDLL("user32").PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._worker is not None:
            self._worker.join(timeout=2)
