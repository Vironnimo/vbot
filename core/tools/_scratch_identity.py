"""Read immutable Linux directory creation times for Scratch identity checks."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

_AT_FDCWD = -100
_AT_SYMLINK_NOFOLLOW = 0x100
_STATX_TYPE = 0x1
_STATX_INO = 0x100
_STATX_BTIME = 0x800


class _Timestamp(ctypes.Structure):
    _fields_ = [
        ("seconds", ctypes.c_int64),
        ("nanoseconds", ctypes.c_uint32),
        ("reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    # Linux UAPI struct statx has a fixed 256-byte layout. Keep its unused tail:
    # the kernel writes the entire structure, including fields we did not ask for.
    _fields_ = [
        ("mask", ctypes.c_uint32),
        ("block_size", ctypes.c_uint32),
        ("attributes", ctypes.c_uint64),
        ("links", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("mode", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("inode", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("blocks", ctypes.c_uint64),
        ("attributes_mask", ctypes.c_uint64),
        ("access", _Timestamp),
        ("birth", _Timestamp),
        ("change", _Timestamp),
        ("modification", _Timestamp),
        ("rdev_major", ctypes.c_uint32),
        ("rdev_minor", ctypes.c_uint32),
        ("dev_major", ctypes.c_uint32),
        ("dev_minor", ctypes.c_uint32),
        ("unused_tail", ctypes.c_uint64 * 14),
    ]


_statx = None
if sys.platform == "linux":
    _libc = ctypes.CDLL(None, use_errno=True)
    _statx = getattr(_libc, "statx", None)
    if _statx is not None:
        _statx.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_Statx),
        )
        _statx.restype = ctypes.c_int


def linux_birthtime_ns(path: Path, status: os.stat_result) -> int | None:
    """Return birth time for the lstat object, or None when unsupported.

    Python's Linux stat_result omits birth time even when the filesystem supplies
    it through statx. Unsupported hosts/filesystems keep the existing best-effort
    inode identity; access errors and replacement races remain setup failures.
    """
    if sys.platform != "linux" or _statx is None:
        return None
    result = _Statx()
    if (
        _statx(
            _AT_FDCWD,
            os.fsencode(path),
            _AT_SYMLINK_NOFOLLOW,
            _STATX_TYPE | _STATX_INO | _STATX_BTIME,
            ctypes.byref(result),
        )
        != 0
    ):
        code = ctypes.get_errno()
        if code in (errno.ENOSYS, errno.EOPNOTSUPP):
            return None
        raise OSError(code, os.strerror(code), str(path))
    if result.mask & _STATX_TYPE and not stat.S_ISDIR(result.mode):
        raise OSError(errno.ENOTDIR, "Scratch path is not a real directory", str(path))
    required = _STATX_TYPE | _STATX_INO | _STATX_BTIME
    if result.mask & required != required:
        return None
    if (result.dev_major, result.dev_minor, result.inode) != (
        os.major(status.st_dev),
        os.minor(status.st_dev),
        status.st_ino,
    ):
        raise OSError(errno.ESTALE, "Scratch directory changed during lookup", str(path))
    return int(result.birth.seconds) * 1_000_000_000 + int(result.birth.nanoseconds)
