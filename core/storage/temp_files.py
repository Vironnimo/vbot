"""Shared lifecycle management for short-lived runtime files."""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from threading import RLock

from core.storage.layout import DataDirectoryLayout
from core.utils.logging import get_logger

_LOGGER = get_logger("storage.temp_files")

TEMPORARY_FILE_RETENTION: Mapping[str, timedelta] = {
    "bash": timedelta(hours=72),
    "subagents": timedelta(hours=24),
}
TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS = 60.0
_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]*$")
_CATEGORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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

    def create(self, category: str, suffix: str) -> TemporaryFileLease:
        """Create and protect one uniquely named file in a fixed category."""
        if category not in self._retention:
            raise ValueError(f"Unknown temporary-file category: {category}")
        if not _SUFFIX_PATTERN.fullmatch(suffix):
            raise ValueError(f"Invalid temporary-file suffix: {suffix!r}")

        category_dir = self.root / category
        category_dir.mkdir(parents=True, exist_ok=True)
        while True:
            path = category_dir / f"{uuid.uuid4().hex}{suffix}"
            try:
                path.touch(exist_ok=False)
            except FileExistsError:
                continue
            break

        with self._lock:
            self._active.add(path)
        return TemporaryFileLease(path=path, _manager=self)

    def start(self) -> None:
        """Sweep crash leftovers now and start periodic cleanup when possible."""
        self.sweep()
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweeper_task = asyncio.create_task(
            self._sweep_loop(),
            name="temporary-files-sweep",
        )

    def stop(self) -> None:
        """Stop periodic cleanup without finishing producer-owned leases."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None

    async def aclose(self) -> None:
        """Stop periodic cleanup and await its task."""
        sweeper_task = self._sweeper_task
        self.stop()
        if sweeper_task is not None and not sweeper_task.done():
            await asyncio.gather(sweeper_task, return_exceptions=True)

    def sweep(self) -> None:
        """Remove expired inactive regular files, isolating filesystem errors."""
        cutoff_epoch = time.time()
        with self._lock:
            active = set(self._active)

        for category, retention in self._retention.items():
            category_dir = self.root / category
            try:
                candidates = list(category_dir.iterdir())
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
            for candidate in candidates:
                if candidate in active:
                    continue
                try:
                    if candidate.is_file() and candidate.stat().st_mtime < expires_before:
                        candidate.unlink()
                except OSError as error:
                    _LOGGER.warning(
                        "Temporary-file cleanup failed path=%s: %s",
                        candidate,
                        error,
                    )

    def _finish(self, path: Path) -> None:
        try:
            os.utime(path, None)
        except OSError as error:
            _LOGGER.warning("Temporary-file completion timestamp failed path=%s: %s", path, error)
        finally:
            with self._lock:
                self._active.discard(path)

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval_seconds)
                self.sweep()
        except asyncio.CancelledError:
            return


__all__ = [
    "TEMPORARY_FILE_RETENTION",
    "TEMPORARY_FILE_SWEEP_INTERVAL_SECONDS",
    "TemporaryFileLease",
    "TemporaryFileManager",
]
