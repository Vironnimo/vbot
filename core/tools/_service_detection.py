"""Find background commands that serve on a TCP port.

A background command whose result is armed wakes its Session when it ends, so
Session activity counts it as pending work. A command that serves - a dev
server, a database - never ends by itself: once any process of its tree listens
on a TCP port, it is a service and no longer counts. The classification is
sticky; a watcher without a port keeps counting.

While at least one such command runs, a poll every ``SERVICE_POLL_SECONDS``
reads the listening sockets once off the Event Loop and intersects their
processes with each command's process tree. Nothing polls while no such command
runs. Sockets the platform keeps private (``psutil.AccessDenied`` and the like)
mean "not detected".
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from core.utils.logging import get_logger

from ._command_record import BlockingRunner, CommandRecord

_LOGGER = get_logger("tools.command_manager")

SERVICE_POLL_SECONDS = 5.0

# Returns the ids of the processes listening on a TCP port now. Blocks.
ListeningReader = Callable[[], frozenset[int]]


class ServiceDetection:
    """Poll for services among the running background commands with an armed result."""

    def __init__(
        self,
        records: Callable[[], Iterable[CommandRecord]],
        *,
        read_listening: ListeningReader,
        run_blocking: BlockingRunner,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._records = records
        self._read_listening = read_listening
        self._run_blocking = run_blocking
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def wake(self) -> None:
        """Start polling when a candidate runs and no poll does."""
        if self._closed or (self._task is not None and not self._task.done()):
            return
        if not self._candidates():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._poll(), name="command-service-detection")
        self._task.add_done_callback(_log_failure)

    def close(self) -> None:
        """Stop polling; also without a running Event Loop."""
        self._closed = True
        if self._task is not None:
            with contextlib.suppress(RuntimeError):
                self._task.cancel()

    async def aclose(self) -> None:
        """Stop polling and wait for the poll to end."""
        self.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    def _candidates(self) -> list[CommandRecord]:
        return [record for record in self._records() if record.awaits_wake and not record.ended]

    async def _poll(self) -> None:
        while not self._closed:
            await self._sleep(SERVICE_POLL_SECONDS)
            candidates = self._candidates()
            if not candidates:
                return
            found: list[CommandRecord] = await self._run_blocking(self._find, candidates)
            for record in found:
                if record.awaits_wake and not record.ended:
                    record.mark_service()

    def _find(self, candidates: list[CommandRecord]) -> list[CommandRecord]:
        """The *candidates* with a process listening on a TCP port. Blocks."""
        try:
            listening = self._read_listening()
        except Exception as error:
            _LOGGER.debug("Listening sockets are unreadable; no service detected: %s", error)
            return []
        if not listening:
            return []
        found = []
        for record in candidates:
            try:
                processes = record.process_ids()
            except Exception as error:
                _LOGGER.debug(
                    "Could not list the processes of command %s: %s", record.command_id, error
                )
                continue
            if processes & listening:
                found.append(record)
        return found


def _log_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.error("Service detection failed", exc_info=task.exception())


__all__ = ["SERVICE_POLL_SECONDS", "ListeningReader", "ServiceDetection"]
