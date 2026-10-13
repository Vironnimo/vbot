"""Linux statx identity preserves immutable creation times and honest failures."""

from __future__ import annotations

import ctypes
import errno
import stat
import sys
from pathlib import Path

import pytest

from core.tools import _scratch_identity as identity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux statx ABI")


@pytest.mark.parametrize(
    "case",
    [
        "birthtime",
        "zero_birthtime",
        "no_birthtime",
        "no_inode",
        "no_type",
        "no_symbol",
        "unsupported_kernel",
        "unsupported_filesystem",
        "permission",
        "io_error",
        "changed_inode",
        "changed_device",
        "changed_type",
    ],
)
def test_linux_birthtime_identifies_the_same_directory_or_reports_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux statx ABI")
    import os

    status = tmp_path.lstat()
    assert ctypes.sizeof(identity._Statx) == 256
    assert identity._Statx.birth.offset == 0x50
    assert identity._Statx.dev_major.offset == 0x88
    errors = {
        "unsupported_kernel": errno.ENOSYS,
        "unsupported_filesystem": errno.EOPNOTSUPP,
        "permission": errno.EACCES,
        "io_error": errno.EIO,
        "changed_type": errno.ENOTDIR,
    }

    def statx(dirfd: int, path: bytes, flags: int, mask: int, output: ctypes.c_void_p) -> int:
        assert (dirfd, path, flags, mask) == (-100, os.fsencode(tmp_path), 0x100, 0x901)
        if case in errors and case != "changed_type":
            ctypes.set_errno(errors[case])
            return -1
        result = ctypes.cast(output, ctypes.POINTER(identity._Statx)).contents
        result.mask = 0x901
        result.mode = stat.S_IFDIR
        result.inode = status.st_ino
        result.dev_major = os.major(status.st_dev)
        result.dev_minor = os.minor(status.st_dev)
        result.birth.seconds = 1_900_000_000
        result.birth.nanoseconds = 987_654_321
        if case == "zero_birthtime":
            result.birth.seconds = result.birth.nanoseconds = 0
        elif case == "no_birthtime":
            result.mask = 0x101
        elif case == "no_inode":
            result.mask = 0x801
        elif case == "no_type":
            result.mask = 0x900
        elif case == "changed_inode":
            result.inode += 1
        elif case == "changed_device":
            result.dev_minor += 1
        elif case == "changed_type":
            result.mode = stat.S_IFLNK
        return 0

    monkeypatch.setattr(identity, "_statx", None if case == "no_symbol" else statx)
    if case in ("permission", "io_error", "changed_inode", "changed_device", "changed_type"):
        with pytest.raises(OSError) as caught:
            identity.linux_birthtime_ns(tmp_path, status)
        assert caught.value.errno == errors.get(case, errno.ESTALE)
    else:
        expected = {"birthtime": 1_900_000_000_987_654_321, "zero_birthtime": 0}.get(case)
        assert identity.linux_birthtime_ns(tmp_path, status) == expected
