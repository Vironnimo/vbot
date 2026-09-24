"""Global Live voice hotkey for the Desktop shell (Windows).

The hotkey is stored as a browser ``KeyboardEvent.code`` plus modifier flags
(see :mod:`desktop.settings`), so the WebUI can capture and show it without a
platform key table. This module owns everything after that:

- the pure mapping and validation of a stored combination
  (:func:`parse_hotkey`),
- one registration thread per active combination (:class:`_HotkeyThread`):
  Windows delivers ``WM_HOTKEY`` only to the thread that called
  ``RegisterHotKey``, so that thread runs its own message loop and also
  unregisters,
- :class:`LiveHotkeyController`, the small surface the launcher and the bridge
  use: persisted preference, current registration, and its error state.

The Win32 calls sit behind :class:`HotkeyApi` so the thread logic is testable
without registering a real system-wide hotkey. Other platforms report the
hotkey as unsupported and never load Win32 code.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from desktop.settings import read_live_hotkey_settings, write_live_hotkey_settings

logger = logging.getLogger("vbot.desktop.hotkey")

HOTKEY_ERROR_IN_USE = "hotkey_in_use"
HOTKEY_ERROR_INVALID = "hotkey_invalid"
HOTKEY_ERROR_FAILED = "hotkey_failed"

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
# Reported when RegisterHotKey fails without setting a last-error value.
HOTKEY_FAILED_WIN32_ERROR = -1

_HOTKEY_ID = 1
_REGISTER_TIMEOUT_SECONDS = 2.0
_STOP_TIMEOUT_SECONDS = 2.0
_MODIFIER_FLAGS = ("ctrl", "alt", "shift", "win")
_SETTING_FLAGS = ("enabled", *_MODIFIER_FLAGS)

# Physical keys (set-1 scan codes) for the letter and digit codes. Windows maps
# them through the active keyboard layout at registration, so the hotkey stays
# on the key the user pressed while capturing it (``KeyY`` is the key labeled
# Z on a German layout). The US virtual key is the fallback.
_LETTER_SCAN_CODES = {
    "A": 0x1E,
    "B": 0x30,
    "C": 0x2E,
    "D": 0x20,
    "E": 0x12,
    "F": 0x21,
    "G": 0x22,
    "H": 0x23,
    "I": 0x17,
    "J": 0x24,
    "K": 0x25,
    "L": 0x26,
    "M": 0x32,
    "N": 0x31,
    "O": 0x18,
    "P": 0x19,
    "Q": 0x10,
    "R": 0x13,
    "S": 0x1F,
    "T": 0x14,
    "U": 0x16,
    "V": 0x2F,
    "W": 0x11,
    "X": 0x2D,
    "Y": 0x15,
    "Z": 0x2C,
}
_DIGIT_SCAN_CODES = {str(digit): 0x02 + (digit - 1) % 10 for digit in range(10)}
_VK_SPACE = 0x20
_VK_F1 = 0x70
_LABELS = (("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"), ("win", "Win"))


@dataclass(frozen=True)
class HotkeySpec:
    """One validated, registrable key combination."""

    ctrl: bool
    alt: bool
    shift: bool
    win: bool
    key: str
    virtual_key: int
    scan_code: int | None = None

    @property
    def modifiers(self) -> int:
        """Return the ``RegisterHotKey`` modifier mask, always without auto-repeat."""

        mask = MOD_NOREPEAT
        if self.ctrl:
            mask |= MOD_CONTROL
        if self.alt:
            mask |= MOD_ALT
        if self.shift:
            mask |= MOD_SHIFT
        if self.win:
            mask |= MOD_WIN
        return mask


def _key_codes(key: str) -> tuple[int, int | None] | None:
    """Return ``(fallback virtual key, scan code)`` for a supported key code."""

    if key == "Space":
        return _VK_SPACE, None
    if key.startswith("Key") and len(key) == 4 and key[3] in _LETTER_SCAN_CODES:
        return ord(key[3]), _LETTER_SCAN_CODES[key[3]]
    if key.startswith("Digit") and len(key) == 6 and key[5] in _DIGIT_SCAN_CODES:
        return ord(key[5]), _DIGIT_SCAN_CODES[key[5]]
    if key.startswith("F") and key[1:].isdigit() and not key[1:].startswith("0"):
        number = int(key[1:])
        if 1 <= number <= 24:
            return _VK_F1 + number - 1, None
    return None


def _needs_modifier(key: str) -> bool:
    """F13-F24 have no other use on common keyboards and may stand alone."""

    return not (key.startswith("F") and key[1:].isdigit() and 13 <= int(key[1:]) <= 24)


def parse_hotkey(setting: Mapping[str, Any]) -> HotkeySpec | None:
    """Return the registrable combination of a stored setting, or ``None``.

    Allowed keys are ``KeyA``-``KeyZ``, ``Digit0``-``Digit9``, ``F1``-``F24``
    and ``Space``. At least one modifier is required unless the key is
    ``F13``-``F24``, so an ordinary typing key can never be captured globally.
    """

    flags: dict[str, bool] = {}
    for name in _MODIFIER_FLAGS:
        value = setting.get(name)
        if not isinstance(value, bool):
            return None
        flags[name] = value
    key = setting.get("key")
    if not isinstance(key, str):
        return None
    codes = _key_codes(key)
    if codes is None:
        return None
    if _needs_modifier(key) and not any(flags.values()):
        return None
    virtual_key, scan_code = codes
    return HotkeySpec(
        ctrl=flags["ctrl"],
        alt=flags["alt"],
        shift=flags["shift"],
        win=flags["win"],
        key=key,
        virtual_key=virtual_key,
        scan_code=scan_code,
    )


class HotkeyApi(Protocol):
    """Thread-affine Win32 calls used by one registration thread."""

    def prepare_thread(self) -> None:
        """Create the calling thread's message queue."""

    def layout_virtual_key(self, scan_code: int) -> int:
        """Map a physical key to the active layout's virtual key (0 if unknown)."""

    def register(self, hotkey_id: int, modifiers: int, virtual_key: int) -> int:
        """Register for the calling thread; return 0 or the Win32 error code."""

    def unregister(self, hotkey_id: int) -> None:
        """Remove the calling thread's registration."""

    def next_message(self) -> tuple[int, int] | None:
        """Block for the next ``(message, wParam)``; ``None`` once the loop must end."""

    def wake(self, thread_id: int) -> bool:
        """Ask the loop of ``thread_id`` to end (posts ``WM_QUIT``)."""


class _Win32HotkeyApi:
    """ctypes binding of :class:`HotkeyApi` with explicit signatures."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._message = wintypes.MSG()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._peek_message = user32.PeekMessageW
        self._peek_message.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._peek_message.restype = wintypes.BOOL
        self._get_message = user32.GetMessageW
        self._get_message.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._get_message.restype = wintypes.BOOL
        self._register_hotkey = user32.RegisterHotKey
        self._register_hotkey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self._register_hotkey.restype = wintypes.BOOL
        self._unregister_hotkey = user32.UnregisterHotKey
        self._unregister_hotkey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._unregister_hotkey.restype = wintypes.BOOL
        self._post_thread_message = user32.PostThreadMessageW
        self._post_thread_message.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._post_thread_message.restype = wintypes.BOOL
        self._map_virtual_key = user32.MapVirtualKeyW
        self._map_virtual_key.argtypes = [wintypes.UINT, wintypes.UINT]
        self._map_virtual_key.restype = wintypes.UINT

    def prepare_thread(self) -> None:
        pm_noremove = 0x0000
        wm_user = 0x0400
        # Any peek creates the thread's message queue, which PostThreadMessageW
        # needs; filtering on WM_USER leaves real messages untouched.
        self._peek_message(self._ctypes.byref(self._message), None, wm_user, wm_user, pm_noremove)

    def layout_virtual_key(self, scan_code: int) -> int:
        mapvk_vsc_to_vk = 1
        return int(self._map_virtual_key(scan_code, mapvk_vsc_to_vk))

    def register(self, hotkey_id: int, modifiers: int, virtual_key: int) -> int:
        if self._register_hotkey(None, hotkey_id, modifiers, virtual_key):
            return 0
        return self._ctypes.get_last_error() or HOTKEY_FAILED_WIN32_ERROR

    def unregister(self, hotkey_id: int) -> None:
        if not self._unregister_hotkey(None, hotkey_id):
            logger.debug("Live voice hotkey was not registered at unregister time")

    def next_message(self) -> tuple[int, int] | None:
        result = self._get_message(self._ctypes.byref(self._message), None, 0, 0)
        if result == 0:
            return None
        if result == -1:
            logger.warning(
                "Live voice hotkey message loop failed (error=%s)",
                self._ctypes.get_last_error(),
            )
            return None
        return int(self._message.message), int(self._message.wParam)

    def wake(self, thread_id: int) -> bool:
        wm_quit = 0x0012
        return bool(self._post_thread_message(thread_id, wm_quit, 0, 0))


class _HotkeyThread:
    """One registration owned by a dedicated message-loop thread."""

    def __init__(
        self,
        spec: HotkeySpec,
        on_press: Callable[[], None],
        api: HotkeyApi,
    ) -> None:
        self._spec = spec
        self._on_press = on_press
        self._api = api
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._cancelled = False
        self._registered = False
        self._thread_id: int | None = None
        self._error_code: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="vbot-live-hotkey",
            daemon=True,
        )

    def start(self, timeout: float = _REGISTER_TIMEOUT_SECONDS) -> str | None:
        """Register on the new thread; return ``None`` or a stable error code."""

        self._thread.start()
        if not self._ready.wait(timeout):
            logger.warning("Live voice hotkey registration did not finish in time")
            self.stop()
            return HOTKEY_ERROR_FAILED
        return self._error_code

    def stop(self) -> None:
        """End the loop, which unregisters on its own thread; idempotent."""

        with self._lock:
            self._cancelled = True
            thread_id = self._thread_id if self._registered else None
        if thread_id is not None and not self._api.wake(thread_id):
            logger.warning("Live voice hotkey thread could not be woken for shutdown")
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=_STOP_TIMEOUT_SECONDS)

    def _run(self) -> None:
        try:
            self._thread_id = threading.get_native_id()
            self._api.prepare_thread()
            virtual_key = self._spec.virtual_key
            if self._spec.scan_code is not None:
                virtual_key = self._api.layout_virtual_key(self._spec.scan_code) or virtual_key
            error = self._api.register(_HOTKEY_ID, self._spec.modifiers, virtual_key)
        except Exception:
            logger.warning("Live voice hotkey registration failed", exc_info=True)
            self._error_code = HOTKEY_ERROR_FAILED
            self._ready.set()
            return
        if error:
            self._error_code = (
                HOTKEY_ERROR_IN_USE
                if error == ERROR_HOTKEY_ALREADY_REGISTERED
                else HOTKEY_ERROR_FAILED
            )
            logger.warning(
                "Live voice hotkey %s could not be registered (error=%s)",
                describe_hotkey(self._spec),
                error,
            )
            self._ready.set()
            return
        with self._lock:
            cancelled = self._cancelled
            self._registered = not cancelled
        if cancelled:
            # The launcher gave up waiting; never keep a system-wide key nobody owns.
            self._api.unregister(_HOTKEY_ID)
            return
        self._ready.set()
        logger.info("Live voice hotkey registered: %s", describe_hotkey(self._spec))
        try:
            self._loop()
        finally:
            self._api.unregister(_HOTKEY_ID)
            logger.info("Live voice hotkey unregistered: %s", describe_hotkey(self._spec))

    def _loop(self) -> None:
        while True:
            message = self._api.next_message()
            if message is None:
                return
            message_id, hotkey_id = message
            if message_id != WM_HOTKEY or hotkey_id != _HOTKEY_ID:
                continue
            try:
                self._on_press()
            except Exception:
                logger.warning("Live voice hotkey handler failed", exc_info=True)


def describe_hotkey(spec: HotkeySpec) -> str:
    """Return a log-friendly ``Ctrl+Alt+Space`` style label."""

    parts = [name for flag, name in _LABELS if getattr(spec, flag)]
    return "+".join([*parts, spec.key])


class LiveHotkeyController:
    """Persisted Live voice hotkey preference plus its live registration.

    Registration is only active between :meth:`start` (after the window is
    shown) and :meth:`stop` (on exit). Every public method is thread-safe and
    idempotent; a failed registration keeps the saved preference and reports
    ``error_code`` so the user can choose another combination.
    """

    def __init__(
        self,
        *,
        settings_path: Path | None,
        on_press: Callable[[], None],
        supported: bool | None = None,
        api_factory: Callable[[], HotkeyApi] | None = None,
    ) -> None:
        self._settings_path = settings_path
        self._on_press = on_press
        self._supported = sys.platform == "win32" if supported is None else supported
        self._api_factory: Callable[[], HotkeyApi] = api_factory or _Win32HotkeyApi
        self._lock = threading.RLock()
        self._active = False
        self._thread: _HotkeyThread | None = None
        self._error_code: str | None = None

    @property
    def supported(self) -> bool:
        """Whether this platform can register a global hotkey."""

        return self._supported

    def status(self) -> dict[str, Any]:
        """Return ``{supported, enabled, hotkey, error_code}`` for the WebUI."""

        with self._lock:
            return self._status(read_live_hotkey_settings(self._settings_path), self._error_code)

    def update(self, changes: Any) -> dict[str, Any]:
        """Merge a partial preference, persist it when valid, and re-register.

        ``changes`` may carry ``enabled``, ``ctrl``, ``alt``, ``shift``, ``win``
        and ``key``. An invalid combination is reported as ``hotkey_invalid``
        and not persisted; turning the hotkey off always succeeds.
        """

        with self._lock:
            current = read_live_hotkey_settings(self._settings_path)
            merged = _merge_hotkey_changes(current, changes)
            if merged is None:
                return self._status(current, HOTKEY_ERROR_INVALID)
            combination_changed = any(name in changes for name in (*_MODIFIER_FLAGS, "key"))
            if (merged["enabled"] or combination_changed) and parse_hotkey(merged) is None:
                return self._status(current, HOTKEY_ERROR_INVALID)
            write_live_hotkey_settings(merged, self._settings_path)
            if self._active:
                self._apply(merged)
            return self._status(merged, self._error_code)

    def start(self) -> None:
        """Begin honoring the saved preference (register it when enabled)."""

        with self._lock:
            if self._active:
                return
            self._active = True
            self._apply(read_live_hotkey_settings(self._settings_path))

    def stop(self) -> None:
        """Unregister and stop the registration thread."""

        with self._lock:
            self._active = False
            self._release()

    def _apply(self, setting: Mapping[str, Any]) -> None:
        self._release()
        self._error_code = None
        if not self._supported or not setting.get("enabled"):
            return
        spec = parse_hotkey(setting)
        if spec is None:
            logger.warning("Saved Live voice hotkey is not a valid combination")
            self._error_code = HOTKEY_ERROR_INVALID
            return
        try:
            api = self._api_factory()
        except Exception:
            logger.warning("Live voice hotkey support could not be loaded", exc_info=True)
            self._error_code = HOTKEY_ERROR_FAILED
            return
        thread = _HotkeyThread(spec, self._on_press, api)
        error_code = thread.start()
        if error_code is None:
            self._thread = thread
        self._error_code = error_code

    def _release(self) -> None:
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.stop()

    def _status(self, setting: Mapping[str, Any], error_code: str | None) -> dict[str, Any]:
        return {
            "supported": self._supported,
            "enabled": bool(setting.get("enabled")),
            "hotkey": {name: setting.get(name) for name in (*_MODIFIER_FLAGS, "key")},
            "error_code": error_code,
        }


def _merge_hotkey_changes(current: Mapping[str, Any], changes: Any) -> dict[str, Any] | None:
    """Apply typed partial changes; ``None`` when a field has the wrong type."""

    if not isinstance(changes, Mapping):
        return None
    merged = dict(current)
    for name in _SETTING_FLAGS:
        if name in changes:
            if not isinstance(changes[name], bool):
                return None
            merged[name] = changes[name]
    if "key" in changes:
        key = changes["key"]
        if not isinstance(key, str) or not key.strip():
            return None
        merged["key"] = key.strip()
    return merged
