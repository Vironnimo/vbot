"""CPU time of individual threads of this process, per platform.

The stall watchdog reads it for every Python thread while a stall lasts, so a
read must cost microseconds and must keep the GIL: a stall is often another
thread holding the GIL, and each release would wait up to a switch interval
(5 ms) to get it back. psutil's per-thread listing is no option either; it
scans the whole system on Windows and takes tens of milliseconds.
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections.abc import Callable

ThreadCpuReader = Callable[[int], float | None]

# Linux encodes a thread's scheduler CPU clock in the clock id:
# MAKE_THREAD_CPUCLOCK(tid, CPUCLOCK_SCHED) in the kernel's posix-timers.h.
_LINUX_CPUCLOCK_SCHED = 2
_LINUX_CPUCLOCK_PERTHREAD = 4


def thread_cpu_reader() -> ThreadCpuReader | None:
    """Return a reader of a thread's CPU seconds by native thread id, if supported.

    The reader returns ``None`` for a thread that no longer exists. Values
    advance in scheduler ticks on Windows (about 15.6 ms).
    """
    if sys.platform == "win32":
        return _windows_reader()
    if sys.platform.startswith("linux") and hasattr(time, "clock_gettime"):
        return _read_linux
    return None


def _windows_reader() -> ThreadCpuReader | None:
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        # PyDLL calls use the C calling convention, which 64-bit Windows shares.
        return None
    from ctypes import wintypes

    try:
        # Unlike WinDLL, PyDLL keeps the GIL during the calls (see module doc).
        kernel32 = ctypes.PyDLL("kernel32")
    except OSError:
        return None
    open_thread = kernel32.OpenThread
    open_thread.restype = wintypes.HANDLE
    open_thread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    # FILETIME is a 64-bit count of 100 ns units in two little-endian halves.
    filetime = ctypes.POINTER(ctypes.c_uint64)
    get_thread_times = kernel32.GetThreadTimes
    get_thread_times.restype = wintypes.BOOL
    get_thread_times.argtypes = (wintypes.HANDLE, filetime, filetime, filetime, filetime)
    close_handle = kernel32.CloseHandle
    close_handle.restype = wintypes.BOOL
    close_handle.argtypes = (wintypes.HANDLE,)
    query_limited_information = 0x0800

    def read(native_id: int) -> float | None:
        handle = open_thread(query_limited_information, False, native_id)
        if not handle:
            return None
        created, exited, kernel, user = (ctypes.c_uint64() for _ in range(4))
        try:
            if not get_thread_times(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
        finally:
            close_handle(handle)
        return (kernel.value + user.value) / 1e7

    return read


def _read_linux(native_id: int) -> float | None:
    if sys.platform == "win32":
        return None
    # The kernel resolves the id per call and rejects a thread that has exited;
    # pthread_getcpuclockid would dereference a possibly freed pthread_t.
    clock_id = (~native_id << 3) | _LINUX_CPUCLOCK_PERTHREAD | _LINUX_CPUCLOCK_SCHED
    try:
        return time.clock_gettime(clock_id)
    except OSError:
        return None
