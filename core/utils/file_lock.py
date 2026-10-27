"""OS-owned exclusive file locks shared by persistence and process owners.

Callers own the descriptor, its lifetime, waiting/retry policy and error mapping.
These primitives never write a sentinel: Windows can lock byte 0 beyond EOF, and
writing it before acquisition races another holder of an empty lock file.
"""

from __future__ import annotations

import os
import sys


def acquire_file_lock(descriptor: int, *, blocking: bool = False) -> None:
    """Lock byte 0 on Windows, or the file on POSIX; propagate OS failures.

    ``blocking`` uses the platform's waiting operation (Windows retries up to
    ten times at one-second intervals); non-blocking callers own their retries.
    The descriptor must stay open until release. Process exit releases the lock.
    """
    os.lseek(descriptor, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(descriptor, mode, 1)
    else:
        import fcntl

        mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(descriptor, mode)


def release_file_lock(descriptor: int) -> None:
    """Release a held lock without closing its descriptor; propagate OS failures."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
