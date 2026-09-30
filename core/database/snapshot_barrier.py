"""Keeps an online data snapshot from capturing half of a compound mutation.

A data snapshot copies the canonical databases one after another and then reads
the JSON document set. A compound mutation changes a database and a durable JSON
document as one unit - an Agent rename retargets Sessions in ``sessions.db`` and
moves ``agents/<id>/agent.json`` - so a capture running between its steps would
publish a snapshot whose members verify one by one but disagree with each other.

The Runtime holds one barrier for its data directory. Every compound mutation
enters it shared (:meth:`SnapshotBarrier.compound_mutation` on a worker thread,
:meth:`SnapshotBarrier.compound_mutation_async` on the Event Loop); any number run
at once and entries nest freely. :func:`core.database.create_data_snapshot` holds
it exclusively (:meth:`SnapshotBarrier.capture`) from the first database copy
through the document capture: it waits until no compound mutation is in flight,
and a compound mutation arriving while the copies are taken waits until they are
done. Ordinary writes never enter the barrier and keep committing.

A compound mutation that arrives while a capture is still waiting enters at once,
so an entry nested inside another (on any thread) never waits on a capture that
waits for its outer entry. The price is that a steady overlap of compound
mutations could starve a capture: it gives up after its wait budget with
``DatabaseUnavailableError``. Holders must not wait for anything a capture holds
(a database transaction or the data-store operation lock) and should stay short.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress

from core.database.errors import DatabaseUnavailableError

#: How long a capture waits for the compound mutations in flight to finish.
SNAPSHOT_BARRIER_WAIT_SECONDS = 30.0
_CANCEL_POLL_SECONDS = 0.1


class SnapshotBarrier:
    """Shared entry for compound mutations, exclusive entry for snapshot capture."""

    def __init__(self, *, capture_wait_seconds: float = SNAPSHOT_BARRIER_WAIT_SECONDS) -> None:
        self._capture_wait_seconds = capture_wait_seconds
        self._condition = threading.Condition()
        self._mutations = 0
        self._capturing = False
        self._async_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    @contextmanager
    def compound_mutation(self) -> Iterator[None]:
        """Run one compound mutation as a unit no capture can split.

        Blocks while a capture copies, so call it off the Event Loop, outside any
        database transaction.
        """
        with self._condition:
            while self._capturing:
                self._condition.wait()
            self._mutations += 1
        try:
            yield
        finally:
            self._leave()

    @asynccontextmanager
    async def compound_mutation_async(self) -> AsyncIterator[None]:
        """:meth:`compound_mutation` for the Event Loop: waits without blocking it."""
        loop = asyncio.get_running_loop()
        while True:
            with self._condition:
                if not self._capturing:
                    self._mutations += 1
                    break
                waiter: asyncio.Future[None] = loop.create_future()
                self._async_waiters.append((loop, waiter))
            await waiter
        try:
            yield
        finally:
            self._leave()

    @contextmanager
    def capture(self, *, cancelled: Callable[[], bool] | None = None) -> Iterator[bool]:
        """Hold off compound mutations while a snapshot copies its members.

        Waits until no compound mutation is in flight and yields ``True`` while
        holding the barrier. Yields ``False`` without holding it once
        ``cancelled`` reports true while waiting (``cancelled`` is only consulted
        while waiting). Raises ``DatabaseUnavailableError`` when no quiet moment
        comes within the wait budget.
        """
        if not self._begin_capture(cancelled):
            yield False
            return
        try:
            yield True
        finally:
            self._end_capture()

    def _begin_capture(self, cancelled: Callable[[], bool] | None) -> bool:
        deadline = time.monotonic() + self._capture_wait_seconds
        with self._condition:
            while self._capturing or self._mutations:
                if cancelled is not None and cancelled():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DatabaseUnavailableError(
                        "an Agent, Project or Session change was still in progress; "
                        "retry the snapshot once it has finished"
                    )
                self._condition.wait(
                    remaining if cancelled is None else min(remaining, _CANCEL_POLL_SECONDS)
                )
            self._capturing = True
            return True

    def _end_capture(self) -> None:
        with self._condition:
            self._capturing = False
            waiters, self._async_waiters = self._async_waiters, []
            self._condition.notify_all()
        for loop, waiter in waiters:
            # A loop that closed meanwhile has no waiter left to wake.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_wake, waiter)

    def _leave(self) -> None:
        with self._condition:
            self._mutations -= 1
            if not self._mutations:
                self._condition.notify_all()


def _wake(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)
