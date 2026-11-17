"""Filesystem privacy of retained Debug traces and speech recordings."""

from __future__ import annotations

import os
import stat
import sys
from contextlib import suppress
from pathlib import Path

from core.storage.layout import DataDirectoryLayout
from core.utils.logging import get_logger
from core.utils.owner_only import OWNER_ONLY_MODE

PRIVATE_FILE_MODE = OWNER_ONLY_MODE if os.name == "posix" else None
_PRIVATE_DIRECTORY_MODE = 0o700
_LOGGER = get_logger("debug")


def ensure_debug_directory(directory: Path) -> None:
    """Prepare a Debug child for writing; no data is written through a link on POSIX."""
    if sys.platform != "win32":
        root = directory.parent
        root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        descriptor = _open_private_directory(root)
        try:
            with suppress(FileExistsError):
                os.mkdir(directory.name, mode=_PRIVATE_DIRECTORY_MODE, dir_fd=descriptor)
            os.close(_open_private_directory(directory.name, dir_fd=descriptor))
        finally:
            os.close(descriptor)
    else:
        directory.mkdir(parents=True, exist_ok=True)


def protect_retained_debug_artifacts(data_dir: str | Path) -> None:
    """Tighten existing POSIX Debug directories and files, even while capture is off."""
    if sys.platform != "win32":
        root = DataDirectoryLayout(data_dir).debug
        try:
            descriptor = _open_private_directory(root)
            try:
                _protect_retained_files(descriptor)
                for name in ("traces", "speech"):
                    try:
                        child = _open_private_directory(name, dir_fd=descriptor)
                    except FileNotFoundError:
                        continue
                    try:
                        _protect_retained_files(child)
                    finally:
                        os.close(child)
            finally:
                os.close(descriptor)
        except FileNotFoundError:
            pass
        except OSError as error:
            _LOGGER.warning(
                "Debug artifact permissions could not be repaired (%s)", type(error).__name__
            )


if sys.platform != "win32":

    def _open_private_directory(path: str | Path, *, dir_fd: int | None = None) -> int:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != _PRIVATE_DIRECTORY_MODE:
                os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _protect_retained_files(descriptor: int) -> None:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    file_descriptor = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    continue
                try:
                    status = os.fstat(file_descriptor)
                    if (
                        stat.S_ISREG(status.st_mode)
                        and status.st_nlink == 1
                        and stat.S_IMODE(status.st_mode) != OWNER_ONLY_MODE
                    ):
                        os.fchmod(file_descriptor, OWNER_ONLY_MODE)
                finally:
                    os.close(file_descriptor)
