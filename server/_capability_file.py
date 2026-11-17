"""Open regular files at the canonical locations authorized by capabilities."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class VerifiedFile:
    path: Path
    stream: BinaryIO
    status: os.stat_result

    def read(self, offset: int, length: int) -> bytes:
        self.stream.seek(offset)
        return self.stream.read(length)

    def close(self) -> None:
        self.stream.close()


def open_canonical_file(path: Path) -> VerifiedFile:
    """Validate the opened file before its bytes are read."""
    if not path.is_absolute() or not path.name or ".." in path.parts or "\0" in str(path):
        raise ValueError("Expected a canonical file path")
    if sys.platform == "win32":
        descriptor, path = _open_windows(path)
    else:
        descriptor = _open_posix(path)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("Expected a regular file")
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    return VerifiedFile(path, stream, status)


if sys.platform != "win32":

    def _open_posix(path: Path) -> int:
        # Each component is opened relative to its already-verified parent.
        # Use search-only handles on Linux so directory listing permission is
        # unnecessary. The final open cannot block on a FIFO.
        directory = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(path.anchor, directory)
        try:
            for part in path.parts[1:-1]:
                child = os.open(part, directory, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,
                dir_fd=descriptor,
            )
        finally:
            os.close(descriptor)


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes
    from functools import cache

    from core.utils.file_status import is_windows_link_tag

    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _SHARE_READ_WRITE_DELETE = 0x7
    _OPEN_EXISTING = 3
    _FILE_TYPE_DISK = 1
    _REPARSE_ATTRIBUTE = 0x400
    _CSTR_EQUAL = 2
    _DOS_PREFIX = "\\\\?\\"
    _UNC_PREFIX = "\\\\?\\UNC\\"

    class _FindData(ctypes.Structure):
        _fields_ = (
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
            ("reserved", wintypes.DWORD),
            ("name", wintypes.WCHAR * 260),
            ("alternate_name", wintypes.WCHAR * 14),
        )
        attributes: int
        reparse_tag: int
        name: str

    @cache
    def _file_api() -> ctypes.WinDLL:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.FindFirstFileW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(_FindData))
        kernel.FindFirstFileW.restype = wintypes.HANDLE
        kernel.FindClose.argtypes = (wintypes.HANDLE,)
        kernel.FindClose.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetFileType.argtypes = (wintypes.HANDLE,)
        kernel.GetFileType.restype = wintypes.DWORD
        kernel.GetFinalPathNameByHandleW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel.CompareStringOrdinal.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.BOOL,
        )
        kernel.CompareStringOrdinal.restype = ctypes.c_int
        return kernel

    def _windows_dos_path(name: str) -> Path:
        if name.startswith(_UNC_PREFIX):
            return Path("\\\\" + name[len(_UNC_PREFIX) :])
        return Path(name.removeprefix(_DOS_PREFIX))

    def _windows_verbatim(path: Path) -> str:
        name = str(path)
        if name.startswith("\\\\"):
            return _UNC_PREFIX + name[2:]
        return _DOS_PREFIX + name

    def _same_windows_name(kernel: ctypes.WinDLL, left: str, right: str) -> bool:
        result = int(kernel.CompareStringOrdinal(left, -1, right, -1, True))
        return result == _CSTR_EQUAL

    def _windows_spelling(kernel: ctypes.WinDLL, path: Path) -> Path:
        # Capture the real spelling for ordinary Windows case aliases. FindFirst
        # describes the last component itself, including symlinks and junctions.
        current = Path(path.anchor)
        for part in path.parts[1:]:
            if any(char in part for char in "*?:"):
                raise ValueError("Expected a canonical Windows file path")
            data = _FindData()
            result = kernel.FindFirstFileW(_windows_verbatim(current / part), ctypes.byref(data))
            if result == _INVALID_HANDLE:
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.FindClose(result)
            link = data.attributes & _REPARSE_ATTRIBUTE and is_windows_link_tag(data.reparse_tag)
            if link or not _same_windows_name(kernel, part, data.name):
                raise ValueError("Expected an unlinked Windows file path")
            current /= data.name
        return current

    def _open_windows(path: Path) -> tuple[int, Path]:
        kernel = _file_api()
        path = _windows_dos_path(str(path))
        if not path.is_absolute() or path.anchor.startswith("\\\\.\\"):
            raise ValueError("Expected a DOS or UNC file path")
        expected = _windows_spelling(kernel, path)
        handle = kernel.CreateFileW(
            _windows_verbatim(path),
            _GENERIC_READ,
            _SHARE_READ_WRITE_DELETE,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if kernel.GetFileType(handle) != _FILE_TYPE_DISK:
                raise ValueError("Expected a disk file")
            size = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
            if not size:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_unicode_buffer(size)
            written = kernel.GetFinalPathNameByHandleW(handle, buffer, size, 0)
            if not written or written >= size:
                raise ctypes.WinError(ctypes.get_last_error())
            actual = _windows_dos_path(buffer.value)
            if actual.parts[1:] != expected.parts[1:] or not _same_windows_name(
                kernel, actual.anchor, expected.anchor
            ):
                raise ValueError("Opened file moved outside the canonical path")
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), actual
        except BaseException:
            kernel.CloseHandle(handle)
            raise
