"""Makes an online data snapshot equal the data a crash at one instant leaves.

A data snapshot copies the canonical databases one after another and reads the
JSON document set, while the process keeps changing them. Two mechanisms make
the copies agree.

**Member freeze.** Every change to a member of a data directory is admitted
through :func:`member_change`: each write transaction of a canonical database
(``Database.write``) and each write or removal of a snapshot document
(``core.json_documents.document_change``). A database spec places its database
(``DatabaseSpec.snapshot_capture``). A capture (:func:`capture_members`) stops
admitting changes to the ``held`` members, every document and every database
but two, waits until the ones in flight have finished (the freeze instant),
and copies the held databases, then the documents, then the ``anchor``, the
Session database, which keeps taking writes so Runs go on. Only then does it
admit held changes again and copy the ``trailing`` members, which keep taking
writes as well. No held member changed between the freeze instant and the
anchor copy, so every held member and the anchor show the data at the anchor
copy's instant: exactly what a crash at that instant would have left. A
trailing member may be newer than that instant, which only data that stays
correct ahead of the anchor may be (Model usage deduplicates its calls and
replays the Session history after a restore).

A held change waits while the members are frozen, except where waiting could
stall or deadlock: on an Event Loop thread, and on a thread that is already
inside a change of the same data directory (a document written inside a
database transaction, or a held write inside a Session transaction). Those
enter at once. One that enters after the freeze instant would break the
copies, so the capture discards them and starts over, up to
:data:`CAPTURE_ATTEMPTS` times before it fails (``DatabaseUnavailableError``).

The freeze state is process-wide and keyed by the resolved data directory,
because it guards files, not objects: every owner, standalone store and
offline tool in the process that changes a member passes the same gate, and
``write_json_document`` has no injected object through which a per-Runtime
freeze could reach its callers (the named exception in PROJECT.md ->
Conventions -> Dependency injection).

**Compound barrier.** A compound mutation changes the Session database and a
held member as one unit - an Agent rename retargets Sessions in ``sessions.db``
and moves ``agents/<id>/agent.json`` - so a capture between its steps would
publish a crash-like state in which half of the operation happened. The
Runtime holds one :class:`SnapshotBarrier` for its data directory. Every
compound mutation enters it shared (:meth:`SnapshotBarrier.compound_mutation`
on a worker thread, :meth:`SnapshotBarrier.compound_mutation_async` on the
Event Loop); any number run at once and entries nest freely. A capture holds it
exclusively (:meth:`SnapshotBarrier.capture`) before the freeze until the
anchor is copied: it waits until no compound mutation is in flight, and one
arriving meanwhile waits until the copies are done.

A compound mutation that arrives while a capture is still waiting enters at
once, so an entry nested inside another (on any thread) never waits on a
capture that waits for its outer entry. The price is that a steady overlap of
compound mutations could starve a capture: it gives up after its wait budget
with ``DatabaseUnavailableError``. Compound mutation holders must not wait for
anything a capture holds (a database transaction or the data-store operation
lock) and should stay short. A thread must not wait for a held change on
another thread while it is inside a change itself: the freeze could not
finish until its drain budget ends and the capture fails.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import (
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
    nullcontext,
    suppress,
)
from dataclasses import dataclass
from pathlib import Path

from core.database.errors import DatabaseUnavailableError
from core.database.spec import (
    ANCHOR_CAPTURE,
    HELD_CAPTURE,
    TRAILING_CAPTURE,
    DatabaseSpec,
    SnapshotCapture,
)

#: How long a capture waits for the compound mutations in flight to finish.
SNAPSHOT_BARRIER_WAIT_SECONDS = 30.0
#: How long a capture waits for the held changes in flight to finish.
MEMBER_DRAIN_WAIT_SECONDS = 10.0
#: How many times a capture is taken before a change during its freeze fails it.
CAPTURE_ATTEMPTS = 3
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


# ---------------------------------------------------------------------------
# Member freeze
# ---------------------------------------------------------------------------


@dataclass
class MemberCapture:
    """What one capture cost the held changes of its data directory."""

    #: From stopping held changes until admitting them again, over every attempt.
    held_seconds: float = 0.0
    #: Held changes that arrived meanwhile and waited.
    waited_changes: int = 0
    #: How many times the members were captured; each retry discarded the copies.
    attempts: int = 1


class _Freeze:
    def __init__(self) -> None:
        self.began = time.monotonic()
        self.frozen = False
        self.disturbed = False
        self.waited_changes = 0
        self.held_seconds = 0.0


# One condition for every data directory: captures are rare, so waking every
# waiting change on a capture's transitions costs nothing worth a lock per
# directory, and the registries stay plain dictionaries keyed by resolved path.
_CONDITION = threading.Condition()
_IN_FLIGHT: dict[Path, int] = {}
_FREEZES: dict[Path, _Freeze] = {}
_THREAD = threading.local()


@contextmanager
def member_change(data_dir: Path, *, held: bool = True) -> Iterator[None]:
    """Admit one change to a member of the resolved ``data_dir``.

    A held change waits while a capture freezes the data directory's held
    members (see the module docstring for the exceptions). ``held=False`` marks
    a change of an anchor or trailing member, which never waits; a held change
    nested inside it enters at once instead of waiting inside its transaction.
    """
    depths = _thread_depths()
    depth = depths.get(data_dir, 0)
    if held:
        _admit(data_dir, exempt=depth > 0 or _on_event_loop())
    depths[data_dir] = depth + 1
    try:
        yield
    finally:
        if depth:
            depths[data_dir] = depth
        else:
            del depths[data_dir]
        if held:
            _leave(data_dir)


def capture_members(
    data_dir: Path,
    databases: Mapping[str, DatabaseSpec | None],
    *,
    copy_database: Callable[[str], None],
    copy_documents: Callable[[], None],
    discard_copies: Callable[[], None],
    barrier: SnapshotBarrier | None = None,
    cancelled: Callable[[], bool] | None = None,
    drain_seconds: float = MEMBER_DRAIN_WAIT_SECONDS,
) -> MemberCapture | None:
    """Copy the members of one data snapshot so that the copies agree.

    ``databases`` names every database member with its spec, which places it
    (``DatabaseSpec.snapshot_capture``); one without a known spec is held. Holds
    ``barrier`` against compound mutations, then freezes the held members and
    copies the held databases (by name), the documents and the anchor; admits
    held changes again and copies the trailing databases. When a change entered
    during the freeze, ``discard_copies`` removes what the attempt copied and
    the capture starts over, at most :data:`CAPTURE_ATTEMPTS` times. Returns
    ``None`` when ``cancelled`` reported true while waiting. Raises
    ``DatabaseUnavailableError`` when compound mutations or held changes did not
    finish within their budgets, or every attempt was disturbed.
    """
    roles: dict[str, SnapshotCapture] = {
        name: HELD_CAPTURE if spec is None else spec.snapshot_capture
        for name, spec in databases.items()
    }
    anchors = sorted(name for name, role in roles.items() if role == ANCHOR_CAPTURE)
    if len(anchors) > 1:
        raise ValueError(f"a data snapshot has one anchor database, not {', '.join(anchors)}")
    held = sorted(name for name, role in roles.items() if role == HELD_CAPTURE)
    key = Path(data_dir).resolve()
    result = MemberCapture(attempts=0)
    while True:
        if result.attempts:
            discard_copies()
        result.attempts += 1
        compound: AbstractContextManager[bool] = (
            nullcontext(True) if barrier is None else barrier.capture(cancelled=cancelled)
        )
        with compound as admitted:
            if not admitted or (freeze := _freeze(key, cancelled, drain_seconds)) is None:
                return None
            try:
                for name in held:
                    copy_database(name)
                copy_documents()
                for name in anchors:
                    copy_database(name)
            finally:
                _thaw(key, freeze)
        result.held_seconds += freeze.held_seconds
        result.waited_changes += freeze.waited_changes
        if not freeze.disturbed:
            break
        if result.attempts >= CAPTURE_ATTEMPTS:
            raise DatabaseUnavailableError(
                "the data changed in a way the snapshot cannot hold while it was taken; "
                "retry the snapshot"
            )
    for name in sorted(name for name, role in roles.items() if role == TRAILING_CAPTURE):
        copy_database(name)
    return result


def _admit(data_dir: Path, *, exempt: bool) -> None:
    with _CONDITION:
        freeze = _FREEZES.get(data_dir)
        if freeze is not None:
            if exempt:
                freeze.disturbed = freeze.disturbed or freeze.frozen
            else:
                freeze.waited_changes += 1
                while data_dir in _FREEZES:
                    _CONDITION.wait()
        _IN_FLIGHT[data_dir] = _IN_FLIGHT.get(data_dir, 0) + 1


def _leave(data_dir: Path) -> None:
    with _CONDITION:
        remaining = _IN_FLIGHT[data_dir] - 1
        if remaining:
            _IN_FLIGHT[data_dir] = remaining
            return
        del _IN_FLIGHT[data_dir]
        if data_dir in _FREEZES:
            _CONDITION.notify_all()


def _freeze(
    data_dir: Path, cancelled: Callable[[], bool] | None, drain_seconds: float
) -> _Freeze | None:
    """Stop admitting held changes and wait until those in flight have finished."""
    freeze = _Freeze()
    deadline = freeze.began + drain_seconds
    with _CONDITION:
        if data_dir in _FREEZES:
            raise DatabaseUnavailableError("another data snapshot of this directory is running")
        _FREEZES[data_dir] = freeze
        try:
            while _IN_FLIGHT.get(data_dir, 0):
                if cancelled is not None and cancelled():
                    _release(data_dir)
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DatabaseUnavailableError(
                        "a data change was still in progress; "
                        "retry the snapshot once it has finished"
                    )
                _CONDITION.wait(
                    remaining if cancelled is None else min(remaining, _CANCEL_POLL_SECONDS)
                )
        except BaseException:
            _release(data_dir)
            raise
        freeze.frozen = True
        return freeze


def _thaw(data_dir: Path, freeze: _Freeze) -> None:
    with _CONDITION:
        freeze.held_seconds = time.monotonic() - freeze.began
        _release(data_dir)


def _release(data_dir: Path) -> None:
    """Admit held changes again; the caller holds ``_CONDITION``."""
    del _FREEZES[data_dir]
    _CONDITION.notify_all()


def _thread_depths() -> dict[Path, int]:
    """How deep the calling thread is inside changes, per data directory."""
    depths: dict[Path, int] | None = getattr(_THREAD, "depths", None)
    if depths is None:
        depths = {}
        _THREAD.depths = depths
    return depths


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
