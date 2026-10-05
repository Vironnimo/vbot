"""Global hotkeys for the Desktop shell (Windows).

A hotkey is stored as a browser ``KeyboardEvent.code`` plus modifier flags under
the ``hotkey`` key of its feature's settings section, so the WebUI can capture
and show it without a platform key table. This module owns everything after
that:

- the pure mapping and validation of a stored combination
  (:func:`parse_hotkey`),
- the stored preference of one hotkey (:class:`HotkeyPreference`,
  :func:`read_hotkey_setting`),
- one registration thread per active combination (:class:`_HotkeyThread`):
  Windows delivers ``WM_HOTKEY`` only to the thread that called
  ``RegisterHotKey``, so that thread runs its own message loop, registers the
  optional Escape key while its owner asks for it, watches a held combination
  until its release, and unregisters,
- :class:`HotkeyController`, the small surface the launcher, the bridge and
  Desktop dictation use: persisted preference, current registration, and its
  error state.

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

from desktop import settings as desktop_settings
from desktop._windows import win32_last_error, win32_library

logger = logging.getLogger("vbot.desktop.hotkey")

HOTKEY_ERROR_IN_USE = "hotkey_in_use"
HOTKEY_ERROR_INVALID = "hotkey_invalid"
HOTKEY_ERROR_FAILED = "hotkey_failed"

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_NULL = 0x0000
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_APP = 0x8000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
# Reported when RegisterHotKey fails without setting a last-error value.
HOTKEY_FAILED_WIN32_ERROR = -1

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_LWIN = 0x5B
VK_RWIN = 0x5C

HOTKEY_ID = 1
ESCAPE_HOTKEY_ID = 2
# Posted to a registration thread: wParam 1 registers Escape, 0 releases it.
WM_ARM_ESCAPE = WM_APP + 1
# How often a held combination is checked for its release.
HOLD_POLL_SECONDS = 0.02

_REGISTER_TIMEOUT_SECONDS = 2.0
_STOP_TIMEOUT_SECONDS = 2.0
_MODIFIER_FLAGS = ("ctrl", "alt", "shift", "win")
_SETTING_FLAGS = ("enabled", *_MODIFIER_FLAGS)
_SETTING_KEY = "hotkey"

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
_FUNCTION_KEYS = {f"F{number}": number for number in range(1, 25)}
_LABELS = (("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"), ("win", "Win"))
# Each held modifier counts as down when any of its virtual keys is down.
_MODIFIER_KEYS = (
    ("ctrl", (VK_CONTROL,)),
    ("alt", (VK_MENU,)),
    ("shift", (VK_SHIFT,)),
    ("win", (VK_LWIN, VK_RWIN)),
)


@dataclass(frozen=True)
class HotkeyPreference:
    """Where one global hotkey is stored and how its logs and thread are named.

    The hotkey lives at ``settings[section]["hotkey"]``; the rest of the section
    belongs to the feature that owns it.
    """

    section: str
    defaults: Mapping[str, Any]
    label: str
    thread_name: str


LIVE_VOICE_HOTKEY = HotkeyPreference(
    section=desktop_settings.LIVE_VOICE_KEY,
    defaults={
        "enabled": False,
        "ctrl": True,
        "alt": True,
        "shift": False,
        "win": False,
        "key": "Space",
    },
    label="Live voice hotkey",
    thread_name="vbot-live-hotkey",
)


def read_hotkey_setting(preference: HotkeyPreference, path: Path | None) -> dict[str, Any]:
    """Return the stored hotkey merged with its defaults.

    Each malformed field falls back to its default independently, so one bad
    hand edit never discards the rest of the preference.
    """

    stored = desktop_settings.read_section(preference.section, path).get(_SETTING_KEY)
    if not isinstance(stored, dict):
        stored = {}
    normalized = dict(preference.defaults)
    for flag in _SETTING_FLAGS:
        if isinstance(stored.get(flag), bool):
            normalized[flag] = stored[flag]
    key = stored.get("key")
    if isinstance(key, str) and key.strip():
        normalized["key"] = key.strip()
    return normalized


def _write_hotkey_setting(
    preference: HotkeyPreference, setting: Mapping[str, Any], path: Path | None
) -> None:
    """Persist the hotkey, preserving the rest of its section and the file."""

    def mutate(section: dict[str, Any]) -> dict[str, Any]:
        section[_SETTING_KEY] = dict(setting)
        return section

    desktop_settings.update_section(preference.section, mutate, path)


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

    def held_keys(self, virtual_key: int) -> tuple[tuple[int, ...], ...]:
        """Return the key groups that stay down while the combination is held.

        ``virtual_key`` is the main key as registered (after the layout
        mapping). A group counts as down when any of its keys is down.
        """

        groups = [keys for flag, keys in _MODIFIER_KEYS if getattr(self, flag)]
        return ((virtual_key,), *groups)


def _key_codes(key: str) -> tuple[int, int | None] | None:
    """Return ``(fallback virtual key, scan code)`` for a supported key code."""

    if key == "Space":
        return _VK_SPACE, None
    if key.startswith("Key") and len(key) == 4 and key[3] in _LETTER_SCAN_CODES:
        return ord(key[3]), _LETTER_SCAN_CODES[key[3]]
    if key.startswith("Digit") and len(key) == 6 and key[5] in _DIGIT_SCAN_CODES:
        return ord(key[5]), _DIGIT_SCAN_CODES[key[5]]
    if key in _FUNCTION_KEYS:
        return _VK_F1 + _FUNCTION_KEYS[key] - 1, None
    return None


def _needs_modifier(key: str) -> bool:
    """F13-F24 have no other use on common keyboards and may stand alone."""

    return not 13 <= _FUNCTION_KEYS.get(key, 0) <= 24


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

    def next_message(self, timeout: float | None) -> tuple[int, int] | None:
        """Wait for the next ``(message, wParam)``; ``None`` once the loop must end.

        With a ``timeout`` (seconds) it returns ``(WM_NULL, 0)`` when nothing
        arrived in time.
        """

    def key_down(self, virtual_key: int) -> bool:
        """Whether the key is physically down right now."""

    def post(self, thread_id: int, message: int, wparam: int) -> bool:
        """Post a message to the loop of ``thread_id`` (``WM_QUIT`` ends it)."""


class _Win32HotkeyApi:
    """ctypes binding of :class:`HotkeyApi` with explicit signatures."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._message = wintypes.MSG()
        user32 = win32_library("user32")
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
        self._wait_for_messages = user32.MsgWaitForMultipleObjects
        self._wait_for_messages.argtypes = [
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._wait_for_messages.restype = wintypes.DWORD
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
        self._get_async_key_state = user32.GetAsyncKeyState
        self._get_async_key_state.argtypes = [ctypes.c_int]
        self._get_async_key_state.restype = ctypes.c_short

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
        return win32_last_error() or HOTKEY_FAILED_WIN32_ERROR

    def unregister(self, hotkey_id: int) -> None:
        if not self._unregister_hotkey(None, hotkey_id):
            logger.debug("Hotkey %s was not registered at unregister time", hotkey_id)

    def next_message(self, timeout: float | None) -> tuple[int, int] | None:
        if timeout is None:
            result = self._get_message(self._ctypes.byref(self._message), None, 0, 0)
            if result == 0:
                return None
            if result == -1:
                logger.warning("Hotkey message loop failed (error=%s)", win32_last_error())
                return None
            return int(self._message.message), int(self._message.wParam)
        # A message already queued does not wake MsgWaitForMultipleObjects, so
        # look before waiting and again after.
        message = self._take_message()
        if message is None:
            qs_allinput = 0x04FF
            self._wait_for_messages(0, None, False, max(0, int(timeout * 1000)), qs_allinput)
            message = self._take_message()
        if message is None:
            return WM_NULL, 0
        if message[0] == WM_QUIT:
            return None
        return message

    def _take_message(self) -> tuple[int, int] | None:
        pm_remove = 0x0001
        if not self._peek_message(self._ctypes.byref(self._message), None, 0, 0, pm_remove):
            return None
        return int(self._message.message), int(self._message.wParam)

    def key_down(self, virtual_key: int) -> bool:
        return bool(self._get_async_key_state(virtual_key) & 0x8000)

    def post(self, thread_id: int, message: int, wparam: int) -> bool:
        return bool(self._post_thread_message(thread_id, message, wparam, 0))


@dataclass(frozen=True)
class HotkeyHandlers:
    """What a registration calls on its own thread; each must return quickly.

    ``on_release`` makes the thread watch every press until a key of the
    combination is let go (also when the owner ignores releases). While it
    watches, an Escape press reaches ``on_escape`` as well, because the
    registered Escape key does not fire while modifiers are held.
    ``on_escape`` is called for Escape only while the owner armed it
    (:meth:`HotkeyController.arm_escape`).
    """

    on_press: Callable[[], None]
    on_release: Callable[[], None] | None = None
    on_escape: Callable[[], None] | None = None


class _HotkeyThread:
    """One registration owned by a dedicated message-loop thread."""

    def __init__(
        self,
        spec: HotkeySpec,
        handlers: HotkeyHandlers,
        api: HotkeyApi,
        preference: HotkeyPreference,
        *,
        escape_armed: bool,
    ) -> None:
        self._spec = spec
        self._handlers = handlers
        self._api = api
        self._label = preference.label
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._cancelled = False
        self._registered = False
        self._thread_id: int | None = None
        self._error_code: str | None = None
        self._escape_wanted = escape_armed
        self._escape_registered = False
        self._thread = threading.Thread(
            target=self._run,
            name=preference.thread_name,
            daemon=True,
        )

    def start(self, timeout: float = _REGISTER_TIMEOUT_SECONDS) -> str | None:
        """Register on the new thread; return ``None`` or a stable error code."""

        self._thread.start()
        if not self._ready.wait(timeout):
            logger.warning("%s registration did not finish in time", self._label)
            self.stop()
            return HOTKEY_ERROR_FAILED
        return self._error_code

    def stop(self) -> None:
        """End the loop, which unregisters on its own thread; idempotent."""

        with self._lock:
            self._cancelled = True
            thread_id = self._thread_id if self._registered else None
        if thread_id is not None and not self._api.post(thread_id, WM_QUIT, 0):
            logger.warning("%s thread could not be woken for shutdown", self._label)
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=_STOP_TIMEOUT_SECONDS)

    def arm_escape(self, armed: bool) -> None:
        """Ask the loop to register (or release) Escape on its own thread."""

        with self._lock:
            thread_id = self._thread_id if self._registered and not self._cancelled else None
        if thread_id is not None and not self._api.post(thread_id, WM_ARM_ESCAPE, int(armed)):
            logger.warning("%s thread could not be asked to arm Escape", self._label)

    def _run(self) -> None:
        try:
            self._thread_id = threading.get_native_id()
            self._api.prepare_thread()
            virtual_key = self._spec.virtual_key
            if self._spec.scan_code is not None:
                virtual_key = self._api.layout_virtual_key(self._spec.scan_code) or virtual_key
            error = self._api.register(HOTKEY_ID, self._spec.modifiers, virtual_key)
        except Exception:
            logger.warning("%s registration failed", self._label, exc_info=True)
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
                "%s %s could not be registered (error=%s)",
                self._label,
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
            self._api.unregister(HOTKEY_ID)
            return
        self._ready.set()
        logger.info("%s registered: %s", self._label, describe_hotkey(self._spec))
        try:
            self._set_escape(self._escape_wanted)
            self._loop(self._spec.held_keys(virtual_key))
        finally:
            self._set_escape(False)
            self._api.unregister(HOTKEY_ID)
            logger.info("%s unregistered: %s", self._label, describe_hotkey(self._spec))

    def _loop(self, held_keys: tuple[tuple[int, ...], ...]) -> None:
        holding = False
        escape_down = False
        while True:
            message = self._api.next_message(HOLD_POLL_SECONDS if holding else None)
            if message is None:
                return
            message_id, wparam = message
            if message_id == WM_HOTKEY and wparam == HOTKEY_ID:
                self._call(self._handlers.on_press)
                if self._handlers.on_release is not None:
                    holding, escape_down = True, False
            elif message_id == WM_HOTKEY and wparam == ESCAPE_HOTKEY_ID:
                if self._escape_registered:
                    self._call(self._handlers.on_escape)
            elif message_id == WM_ARM_ESCAPE:
                self._set_escape(bool(wparam))
            if not holding:
                continue
            # Escape with the combination's modifiers held is a different
            # combination than the registered bare Escape, so watch it here.
            escape_now = self._api.key_down(VK_ESCAPE)
            if escape_now and not escape_down:
                self._call(self._handlers.on_escape)
            escape_down = escape_now
            if not all(any(self._api.key_down(key) for key in group) for group in held_keys):
                holding = False
                self._call(self._handlers.on_release)

    def _set_escape(self, armed: bool) -> None:
        self._escape_wanted = armed
        if armed == self._escape_registered or self._handlers.on_escape is None:
            return
        if not armed:
            self._api.unregister(ESCAPE_HOTKEY_ID)
            self._escape_registered = False
            return
        error = self._api.register(ESCAPE_HOTKEY_ID, MOD_NOREPEAT, VK_ESCAPE)
        if error:
            logger.warning("%s could not claim Escape (error=%s)", self._label, error)
            return
        self._escape_registered = True

    def _call(self, handler: Callable[[], None] | None) -> None:
        if handler is None:
            return
        try:
            handler()
        except Exception:
            logger.warning("%s handler failed", self._label, exc_info=True)


def describe_hotkey(spec: HotkeySpec) -> str:
    """Return a log-friendly ``Ctrl+Alt+Space`` style label."""

    parts = [name for flag, name in _LABELS if getattr(spec, flag)]
    return "+".join([*parts, spec.key])


class HotkeyController:
    """One persisted global hotkey preference plus its live registration.

    Registration is only active between :meth:`start` (after the window is
    shown) and :meth:`stop` (on exit). :meth:`stop` is final: the window's start
    callback can still reach :meth:`start` after the Desktop began shutting down,
    and must not register the global hotkey again. Every public method is
    thread-safe and idempotent; a failed registration keeps the saved preference
    and reports ``error_code`` so the user can choose another combination.
    """

    def __init__(
        self,
        *,
        preference: HotkeyPreference,
        settings_path: Path | None,
        handlers: HotkeyHandlers,
        supported: bool | None = None,
        api_factory: Callable[[], HotkeyApi] | None = None,
    ) -> None:
        self._preference = preference
        self._settings_path = settings_path
        self._handlers = handlers
        self._supported = sys.platform == "win32" if supported is None else supported
        self._api_factory: Callable[[], HotkeyApi] = api_factory or _Win32HotkeyApi
        self._lock = threading.RLock()
        self._active = False
        self._stopped = False
        self._thread: _HotkeyThread | None = None
        self._error_code: str | None = None
        self._escape_armed = False

    @property
    def supported(self) -> bool:
        """Whether this platform can register a global hotkey."""

        return self._supported

    def status(self) -> dict[str, Any]:
        """Return ``{supported, enabled, hotkey, error_code}`` for the WebUI."""

        with self._lock:
            return self._status(self._read(), self._error_code)

    def update(self, changes: Any) -> dict[str, Any]:
        """Merge a partial preference, persist it when valid, and re-register.

        ``changes`` may carry ``enabled``, ``ctrl``, ``alt``, ``shift``, ``win``
        and ``key``; other keys are ignored. An invalid combination is reported
        as ``hotkey_invalid`` and not persisted; turning the hotkey off always
        succeeds.
        """

        with self._lock:
            current = self._read()
            merged = _merge_hotkey_changes(current, changes)
            if merged is None:
                return self._status(current, HOTKEY_ERROR_INVALID)
            combination_changed = any(name in changes for name in (*_MODIFIER_FLAGS, "key"))
            if (merged["enabled"] or combination_changed) and parse_hotkey(merged) is None:
                return self._status(current, HOTKEY_ERROR_INVALID)
            _write_hotkey_setting(self._preference, merged, self._settings_path)
            if self._active:
                self._apply(merged)
            return self._status(merged, self._error_code)

    def start(self) -> None:
        """Begin honoring the saved preference (register it when enabled)."""

        with self._lock:
            if self._active or self._stopped:
                return
            self._active = True
            self._apply(self._read())

    def stop(self) -> None:
        """Unregister and stop the registration thread for good."""

        with self._lock:
            self._active = False
            self._stopped = True
            self._release()

    def arm_escape(self, armed: bool) -> None:
        """Claim Escape globally (``on_escape``) until disarmed; idempotent.

        Kept across a re-registration; a no-op without ``on_escape`` or while
        nothing is registered.
        """

        with self._lock:
            if armed == self._escape_armed:
                return
            self._escape_armed = armed
            if self._thread is not None:
                self._thread.arm_escape(armed)

    def _read(self) -> dict[str, Any]:
        return read_hotkey_setting(self._preference, self._settings_path)

    def _apply(self, setting: Mapping[str, Any]) -> None:
        self._release()
        self._error_code = None
        if not self._supported or not setting.get("enabled"):
            return
        spec = parse_hotkey(setting)
        if spec is None:
            logger.warning("Saved %s is not a valid combination", self._preference.label)
            self._error_code = HOTKEY_ERROR_INVALID
            return
        try:
            api = self._api_factory()
        except Exception:
            logger.warning("%s support could not be loaded", self._preference.label, exc_info=True)
            self._error_code = HOTKEY_ERROR_FAILED
            return
        thread = _HotkeyThread(
            spec, self._handlers, api, self._preference, escape_armed=self._escape_armed
        )
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
