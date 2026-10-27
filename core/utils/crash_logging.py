"""Process-lifetime fatal-error logging shared by the server and Desktop."""

from __future__ import annotations

import faulthandler
import logging
import os
from contextlib import suppress
from pathlib import Path

CRASH_LOG_ROTATE_BYTES = 1024 * 1024


def enable_crash_log(path: Path, header: str, *, logger: logging.Logger) -> None:
    """Append a process start and keep native crash dumps until interpreter exit.

    The caller owns the path and start-line text. The fault handler keeps the
    unbuffered file referenced through interpreter finalization, where native
    libraries can still crash.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A missing file needs nothing; one held by another process on Windows
        # cannot move and keeps growing until a later start.
        with suppress(OSError):
            if path.stat().st_size > CRASH_LOG_ROTATE_BYTES:
                os.replace(path, path.with_name(f"{path.name}.1"))
        crash_log = path.open("ab", buffering=0)
        try:
            crash_log.write(header.encode("utf-8"))
        except OSError:
            crash_log.close()
            raise
    except OSError as exc:
        logger.warning("Opening the crash log failed (path=%s error=%s)", path, exc)
        return
    faulthandler.enable(file=crash_log, all_threads=True, c_stack=True)
