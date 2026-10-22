"""Native Windows file state for the file Tool tests: metadata, and files other programs hold."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Literal


def windows_metadata(
    path: Path, *, protected: bool | None = None, modern: bool = False
) -> tuple[int, str]:
    """Optionally install an explicit DACL, then read flags and the DACL as SDDL."""
    if sys.platform != "win32":
        raise RuntimeError("Windows metadata is available only on Windows")
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel.LocalFree.argtypes = (pointer,)
    kernel.LocalFree.restype = pointer
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        ctypes.POINTER(wintypes.DWORD),
    )
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    security.SetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, pointer)
    security.SetFileSecurityW.restype = wintypes.BOOL
    security.GetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        pointer,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    security.GetFileSecurityW.restype = wintypes.BOOL
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        pointer,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    )
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    kernel.SetFileAttributesW.restype = wintypes.BOOL

    if protected is not None:
        descriptor = pointer()
        # The file's owner keeps full access; an explicit Everyone-read entry
        # distinguishes this from the directory's inherited DACL.
        sddl = "D:" + ("P" if protected else "") + "(A;;FA;;;OW)(A;;FR;;;WD)"
        assert security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        )
        try:
            protection = 0x80000000 if protected else 0x20000000
            assert security.SetFileSecurityW(str(path), 4 | protection, descriptor)
            if modern:
                security.GetSecurityDescriptorDacl.argtypes = (
                    pointer,
                    ctypes.POINTER(wintypes.BOOL),
                    ctypes.POINTER(pointer),
                    ctypes.POINTER(wintypes.BOOL),
                )
                security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
                security.SetNamedSecurityInfoW.argtypes = (
                    wintypes.LPWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    pointer,
                    pointer,
                    pointer,
                    pointer,
                )
                security.SetNamedSecurityInfoW.restype = wintypes.DWORD
                present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), pointer()
                assert security.GetSecurityDescriptorDacl(
                    descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
                )
                assert not security.SetNamedSecurityInfoW(
                    str(path), 1, 4 | protection, None, None, dacl, None
                )
        finally:
            kernel.LocalFree(descriptor)
        # Hidden, system and not-content-indexed, alongside the existing flags.
        assert kernel.SetFileAttributesW(str(path), path.stat().st_file_attributes | 0x2006)

    size = wintypes.DWORD()
    security.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    assert size.value
    buffer = ctypes.create_string_buffer(size.value)
    assert security.GetFileSecurityW(str(path), 4, buffer, size.value, ctypes.byref(size))
    text = wintypes.LPWSTR()
    assert security.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        buffer, 1, 4, ctypes.byref(text), None
    )
    try:
        return path.stat().st_file_attributes, text.value or ""
    finally:
        kernel.LocalFree(text)


@contextmanager
def held_by_another_program(path: Path, how: Literal["open", "lock"]) -> Iterator[None]:
    """Hold ``path`` as another program would while the block runs.

    ``open`` keeps it open without sharing, so opening it fails; ``lock`` locks its
    first byte, so opening it succeeds but reading fails. Windows reports both to
    Python as a bare "permission denied".
    """
    if sys.platform != "win32":
        raise RuntimeError("Sharing and lock violations exist only on Windows")
    import msvcrt

    if how == "lock":
        with path.open("r+b") as stream:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
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
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    generic_read, share_none, open_existing, normal = 0x80000000, 0, 3, 0x80
    handle = kernel.CreateFileW(
        str(path), generic_read, share_none, None, open_existing, normal, None
    )
    assert handle != ctypes.c_void_p(-1).value
    try:
        yield
    finally:
        kernel.CloseHandle(handle)
