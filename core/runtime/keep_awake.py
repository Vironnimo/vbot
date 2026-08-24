"""Opt-in keep-awake: prevent automatic system sleep while the server runs.

When the ``keep_awake`` Settings key is enabled, the running server holds a
process-level Windows power request (``PowerRequestSystemRequired``) so the OS
does not enter idle sleep while vBot is up. This needs no elevation and does
not affect user-initiated sleep. On platforms without the power-request API the
controller is a silent no-op, so the Linux deployment target ignores the flag.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Protocol, cast

# POWER_REQUEST_TYPE value PowerRequestSystemRequired from winnt.h.
_POWER_REQUEST_SYSTEM_REQUIRED = 1
# REASON_CONTEXT version 0 with the simple-string reason variant.
_REASON_CONTEXT_VERSION = 0
_REASON_CONTEXT_SIMPLE_STRING = 1


class _DetailedReason(ctypes.Structure):
    _fields_ = [
        ("module", ctypes.c_void_p),
        ("line", ctypes.c_ulong),
        ("service_name_len", ctypes.c_ulong),
    ]


class _ReasonUnion(ctypes.Union):
    _fields_ = [("simple_string", ctypes.c_wchar_p), ("detailed", _DetailedReason)]


class _ReasonContext(ctypes.Structure):
    _anonymous_ = ("reason",)
    _fields_ = [
        ("version", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("reason", _ReasonUnion),
    ]


class LoggerProtocol(Protocol):
    """Minimal logger surface the controller needs."""

    def debug(self, msg: str, *args: Any) -> None: ...

    def info(self, msg: str, *args: Any) -> None: ...

    def warning(self, msg: str, *args: Any) -> None: ...


def _power_request_supported() -> bool:
    return sys.platform == "win32"


def _load_power_request_api() -> Any:
    library = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    library.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
    library.PowerCreateRequest.restype = ctypes.c_void_p
    library.PowerSetRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.PowerSetRequest.restype = ctypes.c_int
    library.PowerClearRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.PowerClearRequest.restype = ctypes.c_int
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_int
    return library


def _acquire() -> int | None:
    """Create a system-required power request handle, or ``None`` on failure."""

    if not _power_request_supported():
        return None
    try:
        library = _load_power_request_api()
    except OSError as error:
        raise OSError(f"could not load kernel32 power-request API: {error}") from error
    context = _ReasonContext()
    context.version = _REASON_CONTEXT_VERSION
    context.flags = _REASON_CONTEXT_SIMPLE_STRING
    context.simple_string = "vBot"
    # INVALID_HANDLE_VALUE (-1) signals failure; any other non-null handle wins.
    handle = cast(
        "int | None",
        library.PowerCreateRequest(ctypes.byref(context)),
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    if not library.PowerSetRequest(handle, _POWER_REQUEST_SYSTEM_REQUIRED):
        library.CloseHandle(handle)
        return None
    return handle


def _release(handle: int) -> bool:
    """Clear and close a previously acquired power request handle."""

    if not _power_request_supported():
        return False
    try:
        library = _load_power_request_api()
    except OSError as error:
        raise OSError(f"could not load kernel32 power-request API: {error}") from error
    cleared = bool(library.PowerClearRequest(handle, _POWER_REQUEST_SYSTEM_REQUIRED))
    closed = bool(library.CloseHandle(handle))
    return cleared and closed


class KeepAwakeController:
    """Holds or releases the system-required power request for this process."""

    def __init__(self, logger: LoggerProtocol | None = None) -> None:
        self._logger = logger
        self._handle: int | None = None
        self._active = False

    @property
    def active(self) -> bool:
        """Whether the power request is currently held."""

        return self._active

    def set_enabled(self, enabled: bool) -> None:
        """Apply the desired keep-awake state; repeated calls are no-ops."""

        if enabled:
            self._enable()
            return
        self._disable()

    def close(self) -> None:
        """Release the request; safe to call repeatedly and before start."""

        self.set_enabled(False)

    def _log(self, level: str, message: str) -> None:
        if self._logger is not None:
            getattr(self._logger, level)(message)

    def _enable(self) -> None:
        if self._active:
            return
        try:
            handle = _acquire()
        except OSError as error:
            self._log(
                "warning",
                f"Keep-awake could not be activated: {error}",
            )
            return
        if handle is None:
            if _power_request_supported():
                self._log(
                    "warning",
                    "Keep-awake requested but Windows refused the power request",
                )
            else:
                self._log(
                    "debug",
                    "Keep-awake requested but this platform has no power-request API",
                )
            return
        self._handle = handle
        self._active = True
        self._log("info", "Keep-awake active: automatic system sleep prevented")

    def _disable(self) -> None:
        if not self._active:
            return
        handle, self._handle, self._active = self._handle, None, False
        assert handle is not None
        try:
            released = _release(handle)
        except OSError as error:
            self._log("warning", f"Keep-awake release failed: {error}")
            return
        if released:
            self._log("info", "Keep-awake inactive: system may sleep again")
        else:
            self._log("warning", "Keep-awake release was rejected by the platform")
