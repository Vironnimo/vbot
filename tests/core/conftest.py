"""Core test isolation and shared fakes.

The shared retry backoff does not wait, and ``deny_access`` makes folders
unreadable the way the filesystem does.
"""

from __future__ import annotations

import asyncio
import builtins
import errno
import io
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


async def _skip_backoff_wait(_delay: float) -> None:
    # Yield once like a real wait so concurrent tasks still interleave.
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _no_retry_backoff_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip ``core.utils.retry`` backoff waits in every core test.

    A test that observes the waits patches ``core.utils.retry._sleep`` itself.
    """
    monkeypatch.setattr("core.utils.retry._sleep", _skip_backoff_wait)


@pytest.fixture
def deny_access(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path], None]:
    """``deny_access(path)`` makes a folder or file unreadable like one without permission.

    A denied folder's own entry stays visible, but listing it fails; every ``stat``
    of a path inside it, or of a denied file, fails with ``PermissionError``, and so
    does every ``open`` for reading. The existence checks of ``pathlib`` and
    ``os.path`` then answer ``False`` for those paths, as Python 3.14 does when a
    check fails, instead of raising. Writing still works, so code that takes an
    entry it cannot check for missing replaces it. ``monkeypatch.undo()`` makes
    everything readable again.
    """

    denied: list[Path] = []
    denied_files: list[Path] = []
    real_stat, real_lstat, real_scandir = os.stat, os.lstat, os.scandir
    real_open, real_os_open = io.open, os.open

    def location(path: Any) -> Path | None:
        if isinstance(path, int):
            return None
        return Path(os.path.abspath(os.fsdecode(path)))

    def inside(path: Any) -> bool:
        candidate = location(path)
        return candidate is not None and (
            candidate in denied_files or any(folder in candidate.parents for folder in denied)
        )

    def refuse(path: Any) -> PermissionError:
        return PermissionError(errno.EACCES, "Access is denied", os.fsdecode(path))

    def guarded_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if inside(path):
            raise refuse(path)
        return real_stat(path, *args, **kwargs)

    def guarded_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if inside(path):
            raise refuse(path)
        return real_lstat(path, *args, **kwargs)

    def guarded_scandir(path: Any = ".") -> Any:
        if location(path) in denied or inside(path):
            raise refuse(path)
        return real_scandir(path)

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if inside(file) and not any(flag in mode for flag in "wax+"):
            raise refuse(file)
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if inside(path) and not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise refuse(path)
        return real_os_open(path, flags, *args, **kwargs)

    def answer_false_inside(check: Callable[..., bool]) -> Callable[..., bool]:
        def guarded(path: Any, *args: Any, **kwargs: Any) -> bool:
            return False if inside(path) else check(path, *args, **kwargs)

        return guarded

    def deny(path: Path) -> None:
        target = Path(os.path.abspath(path))
        is_file = target.is_file()
        if os.stat is not guarded_stat:
            denied.clear()
            denied_files.clear()
            monkeypatch.setattr(os, "stat", guarded_stat)
            monkeypatch.setattr(os, "lstat", guarded_lstat)
            monkeypatch.setattr(os, "scandir", guarded_scandir)
            monkeypatch.setattr(os, "open", guarded_os_open)
            monkeypatch.setattr(io, "open", guarded_open)
            monkeypatch.setattr(builtins, "open", guarded_open)
            for name in ("exists", "lexists", "isdir", "isfile"):
                monkeypatch.setattr(os.path, name, answer_false_inside(getattr(os.path, name)))
        (denied_files if is_file else denied).append(target)

    return deny
