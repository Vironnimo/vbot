"""Shared lifecycle management for short-lived runtime files."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from threading import Event, RLock

from core.storage.layout import DataDirectoryLayout
from core.utils.file_status import is_link_status
from core.utils.ids import write_id_file
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("storage.temp_files")

TEMPORARY_FILE_RETENTION: Mapping[str, timedelta] = {
    "commands": timedelta(hours=72),
    "computer_use": timedelta(hours=72),
    "speech": timedelta(hours=72),
    "subagents": timedelta(hours=24),
    "terminals": timedelta(hours=72),
    "web_fetch": timedelta(hours=72),
}
# Categories whose files vBot hands to one Session: each such file lies in the
# folder of the Session it belongs to (``<category>/<owner>/``), so a restricted
# Session may read its own files and no other Session's (``session_artifact_dirs``).
SESSION_ARTIFACT_CATEGORIES: tuple[str, ...] = tuple(TEMPORARY_FILE_RETENTION)
TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS = 60.0
_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]*$")
_CATEGORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_OWNER_PATTERN = re.compile(r"^s[0-9a-f]{31}$")
# A sweep lists every retained file (more than ten thousand on a busy install),
# so background sweeps never run on the Event Loop.
_SWEEP_WORKERS = BoundedWorkerPool(name="temporary-files", max_workers=1)


def artifact_owner(project_id: str | None, agent_id: str, session_id: str) -> str:
    """The folder name of one Session's temporary files, the same in every category.

    Derived from the full Session address, so Sessions of different Agents or
    Projects that share a Session id never share a folder.
    """
    address = "\0".join((project_id or "", agent_id, session_id))
    return "s" + hashlib.sha256(address.encode("utf-8")).hexdigest()[:31]


def session_artifact_dirs(data_dir: str | Path, owner: str) -> tuple[Path, ...]:
    """Every folder holding temporary files vBot handed to the Session ``owner`` names."""
    _require_owner(owner)
    temporary = DataDirectoryLayout(data_dir).temporary
    return tuple(temporary / category / owner for category in SESSION_ARTIFACT_CATEGORIES)


def _require_owner(owner: str) -> None:
    if not _OWNER_PATTERN.fullmatch(owner):
        raise ValueError(f"Invalid temporary-file owner: {owner!r}")


@dataclass(slots=True)
class TemporaryFileLease:
    """One active temporary file protected from cleanup until completion."""

    path: Path
    _manager: TemporaryFileManager = field(repr=False)
    _finished: bool = field(default=False, init=False, repr=False)

    def finish(self) -> None:
        """End active protection and start retention from this moment."""
        if self._finished:
            return
        self._finished = True
        self._manager._finish(self.path)


class TemporaryFileManager:
    """Allocate categorized files and remove expired inactive files."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        retention: Mapping[str, timedelta] | None = None,
        sweep_interval_seconds: float = TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if sweep_interval_seconds <= 0:
            raise ValueError("Temporary-file sweep interval must be positive")

        policies = dict(TEMPORARY_FILE_RETENTION if retention is None else retention)
        if not policies:
            raise ValueError("Temporary-file retention policies must not be empty")
        for category, duration in policies.items():
            if not _CATEGORY_PATTERN.fullmatch(category):
                raise ValueError(f"Invalid temporary-file category: {category!r}")
            if duration <= timedelta(0):
                raise ValueError(f"Retention for {category!r} must be positive")

        self.root = DataDirectoryLayout(data_dir).temporary
        self._retention = policies
        self._sweep_interval_seconds = sweep_interval_seconds
        self._active: set[Path] = set()
        self._lock = RLock()
        self._sweeper_task: asyncio.Task[None] | None = None
        # Set by ``stop`` so an in-flight background sweep ends between files.
        self._stopping = Event()

    def create(self, category: str, suffix: str, *, owner: str | None = None) -> TemporaryFileLease:
        """Create and protect one uniquely named file in a fixed category.

        ``owner`` (:func:`artifact_owner`) puts the file in the folder of the
        Session it belongs to; without one it lies in the category folder itself.
        """
        if category not in self._retention:
            raise ValueError(f"Unknown temporary-file category: {category}")
        if not _SUFFIX_PATTERN.fullmatch(suffix):
            raise ValueError(f"Invalid temporary-file suffix: {suffix!r}")
        directory = self.root / category
        if owner is not None:
            _require_owner(owner)
            directory = directory / owner

        # Under the lock a sweep cannot remove the owner folder between its
        # creation and the file's.
        with self._lock:
            path = write_id_file(directory, "tmp", suffix, b"")
            self._active.add(path)
        return TemporaryFileLease(path=path, _manager=self)

    def reopen(self, path: Path) -> TemporaryFileLease:
        """Protect a file created by :meth:`create` again, recreating it if it was removed.

        A producer that writes one file across several active periods finishes
        its lease after each period and reopens it for the next, so retention
        counts from the end of the latest period. Blocking.
        """
        category = path.parent
        if category.parent != self.root and _OWNER_PATTERN.fullmatch(category.name):
            category = category.parent
        if category.parent != self.root or category.name not in self._cleanup_retention():
            raise ValueError(f"Not a temporary file of this manager: {path}")
        with self._lock:
            self._active.add(path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
            except OSError:
                self._active.discard(path)
                raise
        return TemporaryFileLease(path=path, _manager=self)

    def start(self) -> None:
        """Sweep crash leftovers and start periodic cleanup when possible.

        With a running Event Loop the first and every later sweep run on the
        ``temporary-files`` worker pool; without one the first sweep runs inline
        and no periodic cleanup starts.
        """
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._stopping.clear()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.sweep()
            return
        self._sweeper_task = asyncio.create_task(
            self._sweep_loop(),
            name="temporary-files-sweep",
        )

    def stop(self) -> None:
        """Stop periodic cleanup without finishing producer-owned leases.

        An in-flight background sweep ends after its current file.
        """
        self._stopping.set()
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None

    async def aclose(self) -> None:
        """Stop periodic cleanup and await its task, including an in-flight sweep."""
        sweeper_task = self._sweeper_task
        self.stop()
        if sweeper_task is not None and not sweeper_task.done():
            await asyncio.gather(sweeper_task, return_exceptions=True)

    def sweep(self) -> None:
        """Remove expired inactive regular files, isolating filesystem errors.

        Blocking; async code relies on the periodic background sweep instead.
        """
        self._sweep(None)

    def _sweep(self, stopping: Event | None) -> None:
        cutoff_epoch = time.time()
        with self._lock:
            # Compared like ``Path`` equality: case-insensitive on Windows.
            active = {os.path.normcase(path) for path in self._active}

        for category, retention in self._cleanup_retention().items():
            category_dir = self.root / category
            try:
                for directory in (self.root.parent, self.root, category_dir):
                    status = directory.stat(follow_symlinks=False)
                    if is_link_status(status) or not stat.S_ISDIR(status.st_mode):
                        raise OSError("Temporary-file cleanup requires real directories")
                with os.scandir(category_dir) as listing:
                    entries = list(listing)
            except FileNotFoundError:
                continue
            except OSError as error:
                _LOGGER.warning(
                    "Temporary-file category unavailable category=%s: %s",
                    category,
                    error,
                )
                continue

            expires_before = cutoff_epoch - retention.total_seconds()
            if not self._sweep_files(entries, active, expires_before, stopping):
                return
            for entry in entries:
                if stopping is not None and stopping.is_set():
                    return
                if not _OWNER_PATTERN.fullmatch(entry.name):
                    continue
                self._sweep_owner(Path(entry.path), active, expires_before, stopping)

    def _cleanup_retention(self) -> dict[str, timedelta]:
        """Include the retired shell-log folder under its replacement's policy."""
        policies = dict(self._retention)
        if "commands" in policies:
            policies.setdefault("bash", policies["commands"])
        return policies

    def _sweep_owner(
        self, folder: Path, active: set[str], expires_before: float, stopping: Event | None
    ) -> None:
        """Sweep one Session's folder of a category; remove it once empty and expired."""
        try:
            status = folder.stat(follow_symlinks=False)
            if is_link_status(status) or not stat.S_ISDIR(status.st_mode):
                return
            with os.scandir(folder) as listing:
                entries = list(listing)
        except FileNotFoundError:
            return
        except OSError as error:
            _LOGGER.warning("Temporary-file folder unavailable path=%s: %s", folder, error)
            return
        if not self._sweep_files(entries, active, expires_before, stopping):
            return
        try:
            with self._lock:
                if folder.stat(follow_symlinks=False).st_mtime < expires_before:
                    # Fails while a file is left, which a create may just have added.
                    folder.rmdir()
        except FileNotFoundError:
            return
        except OSError:
            # Not empty: a later sweep removes it after its last file.
            return

    def _sweep_files(
        self,
        entries: list[os.DirEntry[str]],
        active: set[str],
        expires_before: float,
        stopping: Event | None,
    ) -> bool:
        """Remove the expired inactive regular files listed; False once stopping."""
        for entry in entries:
            if stopping is not None and stopping.is_set():
                return False
            if os.path.normcase(entry.path) in active:
                continue
            try:
                # Listing data preselects without a per-file system call on
                # Windows (one stat elsewhere); it can lag behind a file
                # still open elsewhere, so a fresh stat confirms each removal.
                if (
                    not entry.is_file(follow_symlinks=False)
                    or entry.stat(follow_symlinks=False).st_mtime >= expires_before
                ):
                    continue
                candidate = Path(entry.path)
                # A finished file can be reopened after the sweep's initial
                # lease snapshot. Serialize its final check and removal with
                # reopening, which may recreate a file the sweep removed.
                with self._lock:
                    if candidate in self._active:
                        continue
                    current = candidate.stat(follow_symlinks=False)
                    if (
                        not is_link_status(current)
                        and stat.S_ISREG(current.st_mode)
                        and current.st_mtime < expires_before
                    ):
                        candidate.unlink()
            except OSError as error:
                _LOGGER.warning(
                    "Temporary-file cleanup failed path=%s: %s",
                    entry.path,
                    error,
                )
        return True

    def _finish(self, path: Path) -> None:
        try:
            os.utime(path, None)
        except OSError as error:
            _LOGGER.warning("Temporary-file completion timestamp failed path=%s: %s", path, error)
        finally:
            with self._lock:
                self._active.discard(path)

    async def _sweep_loop(self) -> None:
        # A cancelled pool call returns only after the started sweep settles, so
        # ``aclose`` never leaves a sweep deleting files behind it.
        try:
            while True:
                await _SWEEP_WORKERS.run(self._sweep, self._stopping)
                await asyncio.sleep(self._sweep_interval_seconds)
        except asyncio.CancelledError:
            return


__all__ = [
    "SESSION_ARTIFACT_CATEGORIES",
    "TEMPORARY_FILE_RETENTION",
    "TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS",
    "TemporaryFileLease",
    "TemporaryFileManager",
    "artifact_owner",
    "session_artifact_dirs",
]
