"""Ordered raw Terminal persistence, settled before its handle or lease closes."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path
from typing import Any, BinaryIO

from core.storage.temp_files import TemporaryFileLease
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool, finish_despite_cancel, settle_before_cancelling

from ._terminal_state import _log_task_failure

_LOGGER = get_logger("tools.terminal_manager")
_LOG_WORKERS = BoundedWorkerPool(name="terminal_log", max_workers=4)


class TerminalLog:
    """Keep each append/flush ahead of rendering, replay and eventual close.

    Session output owns one append at a time under its state lock. Emergency
    shutdown can request a close synchronously; it is queued behind that append
    and remains an owned Task for the manager's async shutdown to drain.
    """

    def __init__(
        self,
        terminal_id: str,
        path: Path | None,
        handle: BinaryIO | None,
        lease: TemporaryFileLease | None,
    ) -> None:
        self.terminal_id = terminal_id
        self.path = path
        self._handle = handle
        self._lease = lease
        self.size = 0
        self.failed = False
        self.end_line: int | None = None
        self._writing: asyncio.Task[bool] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._close_requested = False
        self._gate = threading.Lock()
        self._worker_writing = False

    @property
    def complete(self) -> bool:
        return self.path is not None and self._handle is not None and not self.failed

    async def append(self, data: bytes) -> bool:
        """Append one admitted chunk; return whether this write ended the log."""
        if self._handle is None or self._close_requested:
            return False
        writing = asyncio.create_task(_LOG_WORKERS.run(self._append, data))
        self._writing = writing
        try:
            return await settle_before_cancelling(writing)
        finally:
            if self._writing is writing:
                self._writing = None

    def _append(self, data: bytes) -> bool:
        with self._gate:
            self._worker_writing = True
        try:
            return self._write(data)
        finally:
            with self._gate:
                self._worker_writing = False
                close_requested = self._close_requested
            # Emergency synchronous shutdown may stop pumping the loop. An
            # already-started worker still closes its own handle after writing.
            if close_requested:
                self._close()

    def _write(self, data: bytes) -> bool:
        handle = self._handle
        if handle is None:
            return False
        try:
            handle.write(data)
            handle.flush()
            self.size += len(data)
        except OSError, ValueError:
            _LOGGER.warning(
                "Terminal output log write failed; the log ends here (terminal=%s)",
                self.terminal_id,
                exc_info=True,
            )
            self._handle = None
            self.failed = True
            with contextlib.suppress(OSError, ValueError):
                handle.close()
            return True
        return False

    def close(self) -> None:
        """Finish the file and its lease after the current append, once."""
        with self._gate:
            if self._close_requested:
                return
            self._close_requested = True
            writing_in_worker = self._worker_writing
        writing = self._writing

        async def finish() -> None:
            if writing is not None:
                await finish_despite_cancel(writing)
            await finish_despite_cancel(_LOG_WORKERS.run(self._close))

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop can have an admitted append here: ordinary async shutdown
            # drains its owned tasks before the Event Loop disappears.
            if not writing_in_worker and (writing is None or writing.done()):
                self._close()
        else:
            self._closing = loop.create_task(
                finish(), name=f"terminal:{self.terminal_id}:log-close"
            )

            self._closing.add_done_callback(
                lambda done: _log_task_failure(
                    done, f"Terminal log close failed for terminal={self.terminal_id}"
                )
            )

    async def wait_closed(self) -> None:
        """Settle the requested close before an async lifecycle operation returns."""
        if self._closing is not None:
            await settle_before_cancelling(self._closing)

    def _close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(OSError, ValueError):
                handle.close()
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.finish()

    def pending_tasks(self) -> list[asyncio.Task[Any]]:
        return [
            task for task in (self._writing, self._closing) if task is not None and not task.done()
        ]
