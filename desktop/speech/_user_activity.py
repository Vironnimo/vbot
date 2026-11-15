"""How long the user has not touched the keyboard or mouse of this computer."""

from __future__ import annotations

import ctypes
import sys


class _LastInputInfo(ctypes.Structure):
    _fields_ = (("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint))


def user_idle_seconds() -> float | None:
    """Return the seconds since the last keyboard or mouse input, or ``None`` when unknown.

    Windows only (``GetLastInputInfo``, the input of this logon session);
    other platforms and a failing query report ``None``.
    """
    if sys.platform != "win32":
        return None
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    # Both values are 32-bit millisecond tick counts that wrap after 49.7 days.
    now_ms = int(ctypes.windll.kernel32.GetTickCount())
    elapsed_ms = (now_ms - int(info.dwTime)) & 0xFFFFFFFF
    return elapsed_ms / 1000.0
