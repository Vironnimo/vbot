"""Session activity: whether a Session is working, waiting for work that wakes it, or idle.

A Session is ``working`` while it has an active Run or queued input. Without
either it is ``waiting`` while something pending will wake it by itself:

- its Sub-Agents whose answers go to it (link ``reply_target == "parent"``)
  while they are working or waiting themselves, or a Run of theirs has not yet
  handed its answer to completion delivery;
- its background commands with an armed result that still run, or whose result
  is on its way to completion delivery (``CommandManager.pending_wakes``; a
  command serving on a TCP port never counts, nor does a Terminal);
- waking results completion delivery holds for it and the Model has not
  answered yet (``TriggerService.pending_completion_wakes``).

Otherwise it is ``idle``. This module aggregates those owners; none of them
knows the others' part.

The state is recomputed on events, never polled: a Run starts or finishes, a
background command changes, completion delivery's held results change, a
Sub-Agent Run is done forwarding, and a Sub-Agent's own state changes, which
recomputes its Parent. A move into ``working`` or ``waiting`` is published at
once; a move to ``idle`` only once the Session stayed idle for
``IDLE_SETTLE_SECONDS`` and a recheck confirms it, so the short hand-offs
between those owners never flicker to ``idle``.

The work span runs from the confirmed leave of ``idle`` to the next confirmed
``idle``, across Runs and waiting; a Run that starts while ``idle`` settles
continues it. The move to ``idle`` carries the finished span
(``FinishedWork``), which ends when the Session became idle, not when that was
confirmed. Spans are process-local.

Every decision runs on the Event Loop; Sub-Agent links are read off it.
``state`` and ``snapshot`` may be called from any thread.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol

from core.sessions import SessionAddress
from core.tools.command_manager import CommandNotFoundError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runs import QueuedRunItem, Run
    from core.subagents.links import SubAgentLink

_LOGGER = get_logger("activity")

# How long a Session must stay idle before ``idle`` is published.
IDLE_SETTLE_SECONDS = 2.0

ActivityName = Literal["working", "waiting", "idle"]


@dataclass(frozen=True, slots=True)
class WaitingFor:
    """What a ``waiting`` Session waits for; all zero in any other state."""

    subagents: int = 0
    commands: int = 0
    results: int = 0

    def __bool__(self) -> bool:
        return bool(self.subagents or self.commands or self.results)


@dataclass(frozen=True, slots=True)
class SessionActivityState:
    """One Session's activity as published."""

    state: ActivityName
    # When the current work span began (UTC); None while idle.
    work_started_at: datetime | None = None
    waiting_for: WaitingFor = WaitingFor()
    # False only while working when every active and queued Run is excluded
    # from Agent activity; waiting always counts.
    contributes_to_agent_activity: bool = True


IDLE: Final = SessionActivityState("idle")


@dataclass(frozen=True, slots=True)
class FinishedWork:
    """The work span a move to ``idle`` ended."""

    started_at: datetime
    finished_at: datetime
    duration_ms: int


SessionActivityCallback = Callable[
    [SessionAddress, SessionActivityState, FinishedWork | None], None
]


class _HasOwner(Protocol):
    @property
    def owner(self) -> SessionAddress: ...


class RunSource(Protocol):
    """``ChatRunManager``: active and queued Runs and their start and finish."""

    def add_run_started_callback(self, callback: Callable[[Run], None]) -> Callable[[], None]: ...

    def add_run_finished_callback(self, callback: Callable[[Run], None]) -> Callable[[], None]: ...

    def add_queue_changed_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]: ...

    def active_run(
        self, *, agent_id: str, session_id: str, project_id: str | None
    ) -> Run | None: ...

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None
    ) -> list[QueuedRunItem]: ...


class CommandSource(Protocol):
    """``CommandManager``: background commands that will wake their Session."""

    def add_changed_callback(self, callback: Callable[[str], None]) -> Callable[[], None]: ...

    def pending_wakes(self, owner: SessionAddress) -> int: ...

    def report(self, command_id: str) -> _HasOwner: ...


class CompletionSource(Protocol):
    """``TriggerService``: results held by completion delivery."""

    def add_completion_changed_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]: ...

    def pending_completion_wakes(self, address: SessionAddress) -> int: ...


class SubAgentSource(Protocol):
    """``SubAgentCoordinator``: Parent links and answer forwarding."""

    def add_forwarded_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]: ...

    def forwarding_pending(self, address: SessionAddress) -> bool: ...

    async def parent_link(self, address: SessionAddress) -> SubAgentLink | None: ...

    async def child_sessions(self, address: SessionAddress) -> list[SessionAddress]: ...

    async def parent_answer_pending(self, address: SessionAddress) -> bool: ...


class _Unreadable:
    pass


_UNREADABLE: Final = _Unreadable()


@dataclass(frozen=True, slots=True)
class _Computed:
    state: ActivityName
    waiting_for: WaitingFor = WaitingFor()
    contributes: bool = True


@dataclass(slots=True)
class _Entry:
    """What this process follows of one Session while it is not settled idle."""

    dirty: bool = False
    revision: int = 0
    # Tell the Parent after the next recompute, also without a change.
    tell_parent: bool = False
    worker: asyncio.Task[None] | None = None
    # Start of the current span: wall clock and monotonic; None while idle.
    span: tuple[datetime, float] | None = None
    # When the Session became idle, while that is not yet confirmed.
    idle_since: tuple[datetime, float] | None = None
    settled: bool = False
    settle: asyncio.Task[None] | None = field(default=None, repr=False)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SessionActivity:
    """Own each Session's activity state and work span in this process."""

    def __init__(
        self,
        *,
        runs: RunSource,
        commands: CommandSource,
        completions: CompletionSource,
        subagents: SubAgentSource,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._runs = runs
        self._commands = commands
        self._completions = completions
        self._subagents = subagents
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._entries: dict[SessionAddress, _Entry] = {}
        # Published non-idle states; read from any thread under the lock.
        self._published: dict[SessionAddress, SessionActivityState] = {}
        self._lock = threading.Lock()
        # Parent of each Session followed now (None: it has none).
        self._parents: dict[SessionAddress, SessionAddress | None] = {}
        self._command_owners: dict[str, SessionAddress] = {}
        self._callbacks: list[SessionActivityCallback] = []
        self._links_failing = False
        self._closed = False
        self._unsubscribers = [
            runs.add_run_started_callback(self._on_run),
            runs.add_run_finished_callback(self._on_run),
            runs.add_queue_changed_callback(self._mark),
            commands.add_changed_callback(self._on_command),
            completions.add_completion_changed_callback(self._mark),
            subagents.add_forwarded_callback(self._on_forwarded),
        ]

    def state(self, address: SessionAddress) -> SessionActivityState:
        """The Session's activity as last published; ``IDLE`` for one never followed."""
        with self._lock:
            return self._published.get(address, IDLE)

    def snapshot(self) -> list[tuple[SessionAddress, SessionActivityState]]:
        """Every Session that is not idle, with its activity."""
        with self._lock:
            return list(self._published.items())

    async def pending_wakes(self, address: SessionAddress) -> WaitingFor:
        """Read pending wakes now, independently of publication and idle settling.

        Goals use this same aggregation as activity, but cannot wait for the
        UI's settle period or overlook a child whose first event is still
        waiting for its activity worker. Source changes during link reads
        invalidate the snapshot and are included before returning.
        """
        while not self._closed:
            revisions: dict[SessionAddress, tuple[_Entry | None, int]] = {}
            pending = await self._waiting_for(
                address, seen=frozenset({address}), revisions=revisions
            )
            if all(
                self._entries.get(followed) is entry
                and (entry is None or entry.revision == revision)
                for followed, (entry, revision) in revisions.items()
            ):
                return pending
        return WaitingFor()

    def add_changed_callback(self, callback: SessionActivityCallback) -> Callable[[], None]:
        """Call *callback* with each published change of a Session's activity.

        It gets the address, the new state and, with a move to ``idle``, the
        finished work span. Returns the unsubscribe function.
        """
        self._callbacks.append(callback)

        def remove() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return remove

    async def aclose(self) -> None:
        """Stop following; nothing is published any more."""
        if self._closed:
            return
        self._closed = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._callbacks.clear()
        tasks = [
            task
            for entry in self._entries.values()
            for task in (entry.worker, entry.settle)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._entries.clear()

    # Events

    def _on_run(self, run: Run) -> None:
        self._mark(SessionAddress(run.project_id, run.agent_id, run.session_id))

    def _on_command(self, command_id: str) -> None:
        previous = self._command_owners.pop(command_id, None)
        owner: SessionAddress | None
        try:
            owner = self._commands.report(command_id).owner
        except CommandNotFoundError:
            owner = None
        if owner is not None:
            self._command_owners[command_id] = owner
            self._mark(owner)
        if previous is not None and previous != owner:
            self._mark(previous)

    def _on_forwarded(self, address: SessionAddress) -> None:
        # The Sub-Agent's own state is unchanged; its Parent counts the forwarding.
        self._mark(address, tell_parent=True)

    def _mark(self, address: SessionAddress, *, tell_parent: bool = False) -> None:
        """Recompute the Session's activity soon."""
        if self._closed:
            return
        entry = self._entries.get(address)
        if entry is None:
            entry = self._entries[address] = _Entry()
        entry.dirty = True
        entry.revision += 1
        entry.tell_parent = entry.tell_parent or tell_parent
        if entry.worker is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        entry.worker = loop.create_task(
            self._work(address, entry), name=f"session-activity:{address.agent_id}"
        )
        entry.worker.add_done_callback(_log_failure)

    # Recompute

    async def _work(self, address: SessionAddress, entry: _Entry) -> None:
        try:
            while entry.dirty and not self._closed:
                entry.dirty = False
                computed = await self._compute(address)
                if entry.dirty:
                    # Reading child links yields to source changes. Never publish
                    # their stale snapshot, especially a completed idle recheck
                    # after a new Run has already continued the work span.
                    continue
                changed = self._apply(address, entry, computed)
                if changed or entry.tell_parent:
                    entry.tell_parent = False
                    await self._tell_parent(address)
        finally:
            entry.worker = None
            if (
                not entry.dirty
                and entry.span is None
                and entry.settle is None
                and self._entries.get(address) is entry
            ):
                del self._entries[address]

    async def _compute(self, address: SessionAddress) -> _Computed:
        active = self._runs.active_run(
            agent_id=address.agent_id, session_id=address.session_id, project_id=address.project_id
        )
        queued = self._runs.list_queued(
            address.agent_id, address.session_id, project_id=address.project_id
        )
        if active is not None or queued:
            contributes = (active is not None and active.contributes_to_agent_activity) or any(
                item.admission.contributes_to_agent_activity for item in queued
            )
            return _Computed("working", contributes=contributes)
        pending = await self._waiting_for(address, seen=frozenset({address}))
        return _Computed("waiting", pending) if pending else _Computed("idle")

    async def _waiting_for(
        self,
        address: SessionAddress,
        *,
        seen: frozenset[SessionAddress],
        revisions: dict[SessionAddress, tuple[_Entry | None, int]] | None = None,
    ) -> WaitingFor:
        if revisions is not None:
            entry = self._entries.get(address)
            revisions.setdefault(address, (entry, entry.revision if entry is not None else 0))
        return WaitingFor(
            subagents=await self._pending_subagents(address, seen=seen, revisions=revisions),
            commands=self._commands.pending_wakes(address),
            results=self._completions.pending_completion_wakes(address),
        )

    def _working(self, address: SessionAddress) -> bool:
        return self._runs.active_run(
            agent_id=address.agent_id,
            session_id=address.session_id,
            project_id=address.project_id,
        ) is not None or bool(
            self._runs.list_queued(
                address.agent_id, address.session_id, project_id=address.project_id
            )
        )

    async def _pending_subagents(
        self,
        address: SessionAddress,
        *,
        seen: frozenset[SessionAddress],
        revisions: dict[SessionAddress, tuple[_Entry | None, int]] | None,
    ) -> int:
        """Count children that still owe this Session an answer.

        Publication follows known active children, including their settle
        periods. Fresh queries read durable links and follow actual wake
        sources instead, even before activity workers have found the tree.
        """
        if revisions is None:
            candidates = [
                (child, await self._read_link(child))
                for child, parent in tuple(self._parents.items())
                if parent == address
                and child not in seen
                and (
                    self.state(child).state != "idle"
                    or self._subagents.forwarding_pending(child)
                    or self._working(child)
                )
            ]
        else:
            candidates = [(child, None) for child in await self._subagents.child_sessions(address)]
        count = 0
        for child, link in candidates:
            if revisions is not None:
                entry = self._entries.get(child)
                revisions.setdefault(child, (entry, entry.revision if entry is not None else 0))
                link = await self._subagents.parent_link(child)
            if child in seen:
                continue
            if isinstance(link, _Unreadable):
                count += 1
                continue
            if link is None or link.parent != address:
                continue
            if link.reply_target != "parent":
                if await self._subagents.parent_answer_pending(child):
                    count += 1
                continue
            if (
                revisions is None
                or self._working(child)
                or self._subagents.forwarding_pending(child)
                or await self._waiting_for(child, seen=seen | {child}, revisions=revisions)
            ):
                count += 1
        return count

    def _apply(self, address: SessionAddress, entry: _Entry, computed: _Computed) -> bool:
        """Apply a recomputed state; whether a change was published."""
        published = self.state(address)
        if computed.state == "idle":
            if published.state == "idle":
                return False
            if entry.idle_since is None:
                entry.idle_since = (self._clock(), self._monotonic())
                entry.settled = False
                entry.settle = asyncio.get_running_loop().create_task(
                    self._settle(address, entry), name=f"session-activity-settle:{address.agent_id}"
                )
                entry.settle.add_done_callback(_log_failure)
                return False
            if not entry.settled or entry.span is None:
                return False
            started_at, started = entry.span
            finished_at, finished = entry.idle_since
            work = FinishedWork(
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=max(0, round((finished - started) * 1000)),
            )
            entry.span = None
            self._end_settle(entry)
            self._publish(address, IDLE, work)
            return True
        self._end_settle(entry)
        if entry.span is None:
            entry.span = (self._clock(), self._monotonic())
        state = SessionActivityState(
            computed.state,
            work_started_at=entry.span[0],
            waiting_for=computed.waiting_for,
            contributes_to_agent_activity=computed.contributes,
        )
        if state == published:
            return False
        self._publish(address, state, None)
        return True

    async def _settle(self, address: SessionAddress, entry: _Entry) -> None:
        await self._sleep(IDLE_SETTLE_SECONDS)
        entry.settle = None
        entry.settled = True
        self._mark(address)

    @staticmethod
    def _end_settle(entry: _Entry) -> None:
        if entry.settle is not None:
            entry.settle.cancel()
            entry.settle = None
        entry.idle_since = None
        entry.settled = False

    def _publish(
        self, address: SessionAddress, state: SessionActivityState, finished: FinishedWork | None
    ) -> None:
        with self._lock:
            if state.state == "idle":
                self._published.pop(address, None)
            else:
                self._published[address] = state
        _LOGGER.debug(
            "Session activity changed (agent=%s session=%s state=%s)",
            address.agent_id,
            address.session_id,
            state.state,
        )
        for callback in list(self._callbacks):
            try:
                callback(address, state, finished)
            except Exception:
                _LOGGER.warning("A Session activity callback failed", exc_info=True)

    # Sub-Agent links

    async def _tell_parent(self, address: SessionAddress) -> None:
        """Recompute the Parent of *address*, if it has one."""
        if address in self._parents:
            parent = self._parents[address]
        else:
            link = await self._read_link(address)
            if isinstance(link, _Unreadable):
                return
            parent = None if link is None else link.parent
            self._parents[address] = parent
        if parent is not None and parent != address:
            self._mark(parent)
        if self.state(address).state == "idle" and not self._subagents.forwarding_pending(address):
            # Nothing of it is pending for the Parent any more.
            self._parents.pop(address, None)

    async def _read_link(self, address: SessionAddress) -> SubAgentLink | None | _Unreadable:
        try:
            link = await self._subagents.parent_link(address)
        except Exception:
            if not self._links_failing:
                self._links_failing = True
                _LOGGER.warning(
                    "Session activity cannot read a Sub-Agent link (agent=%s session=%s)",
                    address.agent_id,
                    address.session_id,
                    exc_info=True,
                )
            return _UNREADABLE
        if self._links_failing:
            self._links_failing = False
            _LOGGER.info("Session activity reads Sub-Agent links again")
        return link


def _log_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        _LOGGER.error("Session activity task %s failed", task.get_name(), exc_info=task.exception())


__all__ = [
    "IDLE",
    "IDLE_SETTLE_SECONDS",
    "ActivityName",
    "CommandSource",
    "CompletionSource",
    "FinishedWork",
    "RunSource",
    "SessionActivity",
    "SessionActivityCallback",
    "SessionActivityState",
    "SubAgentSource",
    "WaitingFor",
]
