"""Ordered, off-loop output-file I/O for ProcessManager's two pipe readers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, BinaryIO

from core.storage.temp_files import TemporaryFileLease, TemporaryFileManager
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

_LOGGER = get_logger("tools.process_manager")
_WORKERS = BoundedWorkerPool(name="process-output", max_workers=4)


class ProcessOutputSpool:
    """One file, fed in order by readers that await each captured chunk.

    Each reader retains at most one chunk while waiting. The per-file lock
    preserves stdout/stderr capture order; the shared pool bounds blocking I/O
    across processes. Only worker calls touch the handle or temporary lease.
    """

    def __init__(self, process_id: str, temporary_files: TemporaryFileManager) -> None:
        self.path: Path | None = None
        self._process_id = process_id
        self._temporary_files = temporary_files
        self._handle: BinaryIO | None = None
        self._lease: TemporaryFileLease | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        await _settle(_WORKERS.run(self._open))

    async def append(self, chunk: bytes) -> None:
        # Shield admission and lock acquisition as well as the actual write:
        # cancellation must not discard bytes already captured in memory.
        await _settle(self._append(chunk))

    async def _append(self, chunk: bytes) -> None:
        async with self._lock:
            if self.path is not None:
                await _WORKERS.run(self._write, chunk)

    async def close(self) -> None:
        await _settle(self._run_close())

    async def _run_close(self) -> None:
        async with self._lock:
            await _WORKERS.run(self._close)

    def _open(self) -> None:
        try:
            self._lease = self._temporary_files.create("bash", ".log")
            self._handle = self._lease.path.open("wb")
        except OSError as error:
            _LOGGER.warning(
                "Process log file unavailable for process=%s: %s", self._process_id, error
            )
            self._close()
            return
        self.path = self._lease.path

    def _write(self, chunk: bytes) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(chunk)
            # Keep the complete output readable while the process runs.
            self._handle.flush()
        except OSError as error:
            _LOGGER.warning(
                "Process log file write failed for process=%s, disabling: %s",
                self._process_id,
                error,
            )
            self.path = None
            self._close()

    def _close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError as error:
                self.path = None
                _LOGGER.warning(
                    "Process log file close failed for process=%s: %s", self._process_id, error
                )
            self._handle = None
        if self._lease is not None:
            self._lease.finish()
            self._lease = None


async def _settle(operation: Coroutine[Any, Any, None]) -> None:
    """Complete accepted file work before propagating even repeated cancellation."""
    task = asyncio.create_task(operation)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # Retrieve an exceptional result without replacing the cancellation.
        with contextlib.suppress(BaseException):
            task.result()
        raise
