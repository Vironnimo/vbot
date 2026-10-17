"""Windows DPAPI: wrap small secrets for the current Windows user.

``protect`` encrypts bytes so that only the same Windows account (on this
computer, or wherever its profile's master keys roam) can decrypt them with
``unprotect``. ``entropy`` binds a blob to its purpose: a blob protected for one
purpose does not decrypt for another. Both calls forbid UI prompts and fail
instead. On other platforms both raise :class:`DpapiError`.
"""

from __future__ import annotations

import sys
from typing import Any

#: CRYPTPROTECT_UI_FORBIDDEN: never prompt, fail instead.
_UI_FORBIDDEN = 0x1


class DpapiError(Exception):
    """DPAPI is unavailable or refused the blob; ``code`` is the Windows error, if any."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def available() -> bool:
    """Whether this platform offers DPAPI."""
    return sys.platform == "win32"


def protect(data: bytes, entropy: bytes) -> bytes:
    """Encrypt ``data`` for the current Windows user, bound to ``entropy``."""
    return _call(data, entropy, protect=True)


def unprotect(blob: bytes, entropy: bytes) -> bytes:
    """Decrypt a blob :func:`protect` made for this user with the same ``entropy``."""
    return _call(blob, entropy, protect=False)


def _call(data: bytes, entropy: bytes, *, protect: bool) -> bytes:
    if not available():
        raise DpapiError("Windows DPAPI is not available on this system")
    import ctypes
    from ctypes import wintypes
    from typing import cast

    # Windows-only ctypes members, typed loosely so other platforms type-check.
    native = cast(Any, ctypes)

    class DataBlob(ctypes.Structure):
        _fields_ = (
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        )

    crypt32 = native.WinDLL("crypt32", use_last_error=True)
    kernel32 = native.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_buffer = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(entropy, len(entropy))
    input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_char)))
    entropy_blob = DataBlob(
        len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char))
    )
    output_blob = DataBlob()
    if not function(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        code = native.get_last_error()
        action = "protect" if protect else "unprotect"
        raise DpapiError(f"Windows DPAPI could not {action} the data (error {code})", code=code)
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
