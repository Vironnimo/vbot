"""Owner-only files: readable and writable by the current OS user alone.

Secrets on disk (the local access token, the device store) must not be
readable by other accounts on the machine. On POSIX the file mode is ``0600``.
On Windows a file inherits its folder's access list, which in a user profile
usually grants Administrators and sometimes other users; :func:`restrict_to_owner`
replaces it with a protected access list that grants full control to the
current user and ``SYSTEM`` only, so later changes to the folder do not widen it.

Atomic writers apply this to their temporary file before writing any content;
the rename that publishes the file keeps its access list.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

OWNER_ONLY_MODE = 0o600


def restrict_to_owner(path: Path) -> None:
    """Make the existing file at ``path`` accessible to the current user only.

    Raises ``OSError`` when the permissions cannot be applied: a secret must not
    be written into a file that stays readable by others.
    """

    if sys.platform == "win32":
        _restrict_windows(path)
    else:
        os.chmod(path, OWNER_ONLY_MODE)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SDDL_REVISION_1 = 1
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1

    def _restrict_windows(path: Path) -> None:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel.LocalFree.restype = ctypes.c_void_p
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.ULONG),
        )
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        advapi.GetSecurityDescriptorDacl.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        )
        advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi.SetNamedSecurityInfoW.argtypes = (
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD

        # "P" protects the list from inheritance; "FA" is full file access.
        sddl = f"D:P(A;;FA;;;{_current_user_sid(advapi, kernel)})(A;;FA;;;SY)"
        descriptor = ctypes.c_void_p()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
            if not advapi.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            error = advapi.SetNamedSecurityInfoW(
                str(path),
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            if error:
                raise ctypes.WinError(error)
        finally:
            kernel.LocalFree(descriptor)

    def _current_user_sid(advapi: ctypes.WinDLL, kernel: ctypes.WinDLL) -> str:
        """The string SID of the user this process runs as."""

        kernel.GetCurrentProcess.argtypes = ()
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        advapi.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi.GetTokenInformation.restype = wintypes.BOOL
        advapi.ConvertSidToStringSidW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        )
        advapi.ConvertSidToStringSidW.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(
                token, _TOKEN_USER, buffer, size.value, ctypes.byref(size)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is the SID pointer.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                value = text.value
            finally:
                kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            kernel.CloseHandle(token)
        if not value:
            raise OSError("The current user's security identifier is unavailable")
        return value
