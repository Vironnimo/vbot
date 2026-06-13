"""Run coordination primitives for session execution."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from core.utils.errors import VBotError

JsonObject = dict[str, Any]
RunExecutor = Callable[["Run"], Awaitable[Any]]
CancelCallback = Callable[[], Any]
_LOGGER = logging.getLogger("vbot.runs")
DEFAULT_RUN_EVENT_RETENTION_LIMIT = 4096
DEFAULT_RUN_SUBSCRIBER_QUEUE_LIMIT = 4096
DEFAULT_COMPLETED_RUN_RETENTION_LIMIT = 512

RUN_STARTED_EVENT = "run_started"
USER_MESSAGE_EVENT = "user_message_persisted"
COMPACTION_COMPLETED_EVENT = "compaction_completed"
REASONING_EVENT = "reasoning"
ASSISTANT_OUTPUT_DELTA_EVENT = "assistant_output_delta"
REASONING_DELTA_EVENT = "reasoning_delta"
TOOL_CALL_DELTA_EVENT = "tool_call_delta"
TOOL_CALL_STDOUT_EVENT = "tool_call_stdout"
TOOL_CALL_STDERR_EVENT = "tool_call_stderr"
TOOL_CALL_STARTED_EVENT = "tool_call_started"
TOOL_CALL_RESULT_EVENT = "tool_call_result"
ASSISTANT_OUTPUT_EVENT = "assistant_output"
ERROR_MESSAGE_PERSISTED_EVENT = "error_message_persisted"
MODEL_FALLBACK_ACTIVATED_EVENT = "model_fallback_activated"
RUN_COMPLETED_EVENT = "run_completed"
RUN_FAILED_EVENT = "run_failed"
RUN_CANCELLED_EVENT = "run_cancelled"
TERMINAL_EVENT_TYPES = {RUN_COMPLETED_EVENT, RUN_FAILED_EVENT, RUN_CANCELLED_EVENT}


class RunStatus(StrEnum):
    """Terminal and active states for a chat run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunError(VBotError):
    """Base error for run coordination failures."""


class ActiveRunError(RunError):
    """Raised when a session already has an active run."""


class RunNotFoundError(RunError):
    """Raised when a run id is unknown."""


class RunCancelledError(RunError):
    """Raised when awaiting a cancelled run."""


@dataclass
class QueuedRunItem:
    """One queued run request waiting for a session turn slot."""

    item_id: str
    display_content: str
    executor: RunExecutor
    internal: bool
    future: asyncio.Future[Run]
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> JsonObject:
        """Return a server-safe queued item dictionary."""
        return {
            "id": self.item_id,
            "content": self.display_content,
            "internal": self.internal,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RunEvent:
    """Provider-agnostic visible event in a run timeline."""

    sequence: int
    run_id: str
    agent_id: str
    session_id: str
    type: str
    payload: JsonObject = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> JsonObject:
        """Return a JSON-compatible event dictionary."""
        return {
            "sequence": self.sequence,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "type": self.type,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
        }


class _LaggedRunSubscriberSentinel:
    """Internal marker that closes a lagging live Run subscriber."""


_LAGGED_RUN_SUBSCRIBER = _LaggedRunSubscriberSentinel()


class _CancelledToolCallSentinel:
    """Internal marker that a per-tool-call cancel was already invoked."""


_CANCELLED_TOOL_CALL = _CancelledToolCallSentinel()


@dataclass
class _RunSubscriber:
    queue: asyncio.Queue[RunEvent | _LaggedRunSubscriberSentinel]
    closed: bool = False


class Run:
    """One active execution inside a persisted chat session."""

    def __init__(
        self,
        *,
        run_id: str,
        agent_id: str,
        session_id: str,
        event_retention_limit: int = DEFAULT_RUN_EVENT_RETENTION_LIMIT,
        subscriber_queue_limit: int = DEFAULT_RUN_SUBSCRIBER_QUEUE_LIMIT,
    ) -> None:
        if event_retention_limit < 1:
            raise ValueError("event_retention_limit must be positive")
        if subscriber_queue_limit < 1:
            raise ValueError("subscriber_queue_limit must be positive")
        self.id = run_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.status = RunStatus.RUNNING
        self.created_at = datetime.now(UTC).isoformat()
        self.updated_at = self.created_at
        self.result: Any | None = None
        self.error: BaseException | None = None
        self.cancel_requested = False
        self.cancel_reason: str | None = None
        self._events: deque[RunEvent] = deque(maxlen=event_retention_limit)
        self._next_sequence = 1
        self._subscribers: list[_RunSubscriber] = []
        self._subscriber_queue_limit = subscriber_queue_limit
        self._done = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._cancel_callbacks: list[CancelCallback] = []
        self._tool_cancel_callbacks: dict[str, CancelCallback | _CancelledToolCallSentinel] = {}
        self._started_from_queue_item_id: str | None = None

    @property
    def events(self) -> list[RunEvent]:
        """Return a replayable snapshot of events emitted so far."""
        return list(self._events)

    def set_task(self, task: asyncio.Task[None]) -> None:
        """Attach the background execution task for cancellation."""
        self._task = task

    def add_cancel_callback(self, callback: CancelCallback) -> None:
        """Register cleanup work to trigger when cancellation is requested."""
        if self.cancel_requested:
            _schedule_callback(callback)
            return
        self._cancel_callbacks.append(callback)

    def request_cancel(self, reason: str | None = None) -> None:
        """Request best-effort cancellation of this run."""
        if self.status != RunStatus.RUNNING or self.cancel_requested:
            return
        self.cancel_reason = reason
        self.cancel_requested = True
        for callback in list(self._cancel_callbacks):
            _schedule_callback(callback)
        if self._task is not None:
            self._task.cancel()

    def register_tool_cancel(self, tool_call_id: str, callback: CancelCallback) -> None:
        """Register a per-tool-call cancel callback without cancelling the run."""
        self._tool_cancel_callbacks[tool_call_id] = callback

    def cancel_tool_call(self, tool_call_id: str) -> bool:
        """Cancel a specific tool call without cancelling the run itself."""
        entry = self._tool_cancel_callbacks.get(tool_call_id)
        if entry is None or entry is _CANCELLED_TOOL_CALL:
            return False
        self._tool_cancel_callbacks[tool_call_id] = _CANCELLED_TOOL_CALL
        _schedule_callback(cast(CancelCallback, entry))
        return True

    def tool_call_cancelled(self, tool_call_id: str) -> bool:
        """Return whether a tool call was user-cancelled."""
        return self._tool_cancel_callbacks.get(tool_call_id) is _CANCELLED_TOOL_CALL

    def clear_tool_cancel(self, tool_call_id: str) -> None:
        """Remove the per-tool-call cancel registry entry."""
        self._tool_cancel_callbacks.pop(tool_call_id, None)

    def raise_if_cancelled(self) -> None:
        """Stop executor progress once cancellation has been requested."""
        if self.cancel_requested:
            raise asyncio.CancelledError

    def emit(self, event_type: str, payload: JsonObject | None = None) -> RunEvent | None:
        """Append and publish one visible run event.

        After cancellation is requested, only terminal events are forwarded. This
        keeps late provider/tool results from becoming visible.
        """
        if self.status != RunStatus.RUNNING and event_type not in TERMINAL_EVENT_TYPES:
            return None
        if self.cancel_requested and event_type not in TERMINAL_EVENT_TYPES:
            return None
        event = RunEvent(
            sequence=self._next_sequence,
            run_id=self.id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            type=event_type,
            payload=dict(payload or {}),
        )
        self._next_sequence += 1
        self._events.append(event)
        self.updated_at = event.timestamp
        for subscriber in list(self._subscribers):
            self._publish_to_subscriber(subscriber, event)
        return event

    async def subscribe(self, *, after_sequence: int = 0) -> AsyncGenerator[RunEvent, None]:
        """Replay old events and stream future events until a terminal event."""
        if self.status != RunStatus.RUNNING:
            for event in list(self._events):
                if event.sequence > after_sequence:
                    yield event
                    if event.type in TERMINAL_EVENT_TYPES:
                        return
            return

        subscriber = _RunSubscriber(queue=asyncio.Queue(maxsize=self._subscriber_queue_limit))
        self._subscribers.append(subscriber)
        try:
            for event in list(self._events):
                if subscriber.closed:
                    return
                if event.sequence > after_sequence:
                    yield event
                    after_sequence = event.sequence
                    if event.type in TERMINAL_EVENT_TYPES:
                        return

            while True:
                item = await subscriber.queue.get()
                if item is _LAGGED_RUN_SUBSCRIBER:
                    return
                event = cast(RunEvent, item)
                if event.sequence <= after_sequence:
                    continue
                yield event
                after_sequence = event.sequence
                if event.type in TERMINAL_EVENT_TYPES:
                    return
        finally:
            self._remove_subscriber(subscriber)

    def _publish_to_subscriber(self, subscriber: _RunSubscriber, event: RunEvent) -> None:
        if subscriber.closed:
            return
        try:
            subscriber.queue.put_nowait(event)
        except asyncio.QueueFull:
            self._evict_lagging_subscriber(subscriber)

    def _evict_lagging_subscriber(self, subscriber: _RunSubscriber) -> None:
        if subscriber.closed:
            return
        self._remove_subscriber(subscriber)
        _drain_queue(subscriber.queue)
        subscriber.queue.put_nowait(_LAGGED_RUN_SUBSCRIBER)
        _LOGGER.warning("Evicted lagging run subscriber for run %s", self.id)

    def _remove_subscriber(self, subscriber: _RunSubscriber) -> None:
        subscriber.closed = True
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    async def wait(self) -> Any:
        """Wait for terminal state and return the run result."""
        await self._done.wait()
        if self.status == RunStatus.CANCELLED:
            raise RunCancelledError(f"run cancelled: {self.id}")
        if self.status == RunStatus.FAILED and self.error is not None:
            raise self.error
        return self.result

    def mark_completed(self, result: Any, payload_extras: JsonObject | None = None) -> None:
        """Move the run to completed and publish the terminal event."""
        if self.status != RunStatus.RUNNING:
            return
        self.result = result
        self.status = RunStatus.COMPLETED
        payload: JsonObject = {"status": self.status.value}
        if payload_extras:
            payload.update(payload_extras)
        self.emit(RUN_COMPLETED_EVENT, payload)
        self._done.set()

    def mark_failed(self, error: BaseException, payload_extras: JsonObject | None = None) -> None:
        """Move the run to failed and publish the terminal event.

        This is the single authoritative failure-log chokepoint: every run
        executor (interactive, cron, channel, subagent) reaches it, so logging
        here guarantees a failed run always leaves a log entry. Expected
        ``VBotError`` failures log at ``warning`` without a traceback; any other
        exception logs at ``error`` with the traceback.
        """
        if self.status != RunStatus.RUNNING:
            return
        self.error = error
        self.status = RunStatus.FAILED
        if isinstance(error, VBotError):
            _LOGGER.warning(
                "Run %s failed (agent=%s session=%s): %s",
                self.id,
                self.agent_id,
                self.session_id,
                error,
            )
        else:
            _LOGGER.error(
                "Run %s failed unexpectedly (agent=%s session=%s)",
                self.id,
                self.agent_id,
                self.session_id,
                exc_info=error,
            )
        payload: JsonObject = {"status": self.status.value, "error": str(error)}
        if payload_extras:
            payload.update(payload_extras)
        self.emit(RUN_FAILED_EVENT, payload)
        self._done.set()

    def mark_cancelled(self, payload_extras: JsonObject | None = None) -> None:
        """Move the run to cancelled and publish the terminal event."""
        if self.status != RunStatus.RUNNING:
            return
        self.status = RunStatus.CANCELLED
        payload: JsonObject = {"status": self.status.value}
        if self.cancel_reason is not None:
            payload["reason"] = self.cancel_reason
        if payload_extras:
            payload.update(payload_extras)
        self.emit(RUN_CANCELLED_EVENT, payload)
        self._done.set()


class ChatRunManager:
    """Coordinates active chat runs across sessions."""

    def __init__(
        self,
        *,
        completed_run_retention_limit: int = DEFAULT_COMPLETED_RUN_RETENTION_LIMIT,
        run_event_retention_limit: int = DEFAULT_RUN_EVENT_RETENTION_LIMIT,
    ) -> None:
        if completed_run_retention_limit < 1:
            raise ValueError("completed_run_retention_limit must be positive")
        if run_event_retention_limit < 1:
            raise ValueError("run_event_retention_limit must be positive")
        self._lock = asyncio.Lock()
        self._active_by_session: dict[tuple[str, str], Run] = {}
        self._queues: dict[tuple[str, str], deque[QueuedRunItem]] = {}
        self._runs: dict[str, Run] = {}
        self._run_started_callbacks: list[Callable[[Run], None]] = []
        self._completed_run_retention_limit = completed_run_retention_limit
        self._run_event_retention_limit = run_event_retention_limit

    def add_run_started_callback(self, callback: Callable[[Run], None]) -> Callable[[], None]:
        """Register a callback invoked whenever this manager starts a Run."""
        self._run_started_callbacks.append(callback)

        def remove_callback() -> None:
            if callback in self._run_started_callbacks:
                self._run_started_callbacks.remove(callback)

        return remove_callback

    async def start(self, *, agent_id: str, session_id: str, executor: RunExecutor) -> Run:
        """Start one run if the session has no active run."""
        session_key = (agent_id, session_id)
        async with self._lock:
            active_run = self._active_by_session.get(session_key)
            if active_run is not None and active_run.status == RunStatus.RUNNING:
                raise ActiveRunError(f"session already has an active run: {session_id}")
            return self._start_run_locked(
                session_key=session_key,
                agent_id=agent_id,
                session_id=session_id,
                executor=executor,
            )

    async def enqueue(
        self,
        *,
        agent_id: str,
        session_id: str,
        executor: RunExecutor,
        display_content: str = "",
        internal: bool = False,
    ) -> QueuedRunItem:
        """Start immediately when idle or append one item to the session queue."""
        session_key = (agent_id, session_id)
        future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        item = QueuedRunItem(
            item_id=str(uuid.uuid4()),
            display_content=display_content,
            executor=executor,
            internal=internal,
            future=future,
        )

        async with self._lock:
            active_run = self._active_by_session.get(session_key)
            if active_run is None or active_run.status != RunStatus.RUNNING:
                run = self._start_run_locked(
                    session_key=session_key,
                    agent_id=agent_id,
                    session_id=session_id,
                    executor=item.executor,
                    queue_item_id=item.item_id,
                )
                item.future.set_result(run)
                return item

            self._queues.setdefault(session_key, deque()).append(item)
            return item

    def list_queued(self, agent_id: str, session_id: str) -> list[QueuedRunItem]:
        """Return queued items for one session in FIFO order."""
        return list(self._queues.get((agent_id, session_id), ()))

    def remove_queued(self, agent_id: str, session_id: str, item_id: str) -> bool:
        """Remove one queued item if present."""
        session_key = (agent_id, session_id)
        queue = self._queues.get(session_key)
        if queue is None:
            return False

        for item in queue:
            if item.item_id != item_id:
                continue
            queue.remove(item)
            if not item.future.done():
                item.future.cancel()
            if not queue:
                self._queues.pop(session_key, None)
            return True
        return False

    def update_queued(
        self,
        agent_id: str,
        session_id: str,
        item_id: str,
        new_executor: RunExecutor,
        new_display_content: str,
    ) -> bool:
        """Replace the queued executor and display text for one item."""
        queue = self._queues.get((agent_id, session_id))
        if queue is None:
            return False

        for item in queue:
            if item.item_id != item_id:
                continue
            item.executor = new_executor
            item.display_content = new_display_content
            return True
        return False

    def get(self, run_id: str) -> Run:
        """Return a run by id."""
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run not found: {run_id}") from exc

    async def cancel(self, run_id: str, reason: str | None = None) -> Run:
        """Request cancellation and wait until the run reaches a terminal state."""
        run = self.get(run_id)
        run.request_cancel(reason=reason)
        await run._done.wait()  # noqa: SLF001 - manager owns run lifecycle internals.
        return run

    def cancel_by_session(self, agent_id: str, session_id: str) -> Run:
        """Request cancellation for the active run in one session."""
        run = self._active_by_session.get((agent_id, session_id))
        if run is None or run.status != RunStatus.RUNNING:
            raise RunNotFoundError(f"no active run for agent '{agent_id}' session '{session_id}'")
        run.request_cancel()
        return run

    def active_run(self, *, agent_id: str, session_id: str) -> Run | None:
        """Return the active run for a session, if one exists."""
        run = self._active_by_session.get((agent_id, session_id))
        if run is None or run.status != RunStatus.RUNNING:
            return None
        return run

    def active_runs(self) -> list[Run]:
        """Return a snapshot of every currently running run across all sessions.

        Mirrors :meth:`active_run` for callers that need the full set (for
        example, the WebSocket handshake snapshot sent to a freshly connected
        client). Entries whose status has moved off ``RUNNING`` since being
        recorded are filtered out; the returned list is a fresh list, so
        callers may mutate it without affecting the manager.
        """
        return [run for run in self._active_by_session.values() if run.status == RunStatus.RUNNING]

    def has_activity_for_agent(self, agent_id: str) -> bool:
        """Return whether an agent owns any running run or queued run item."""
        for (active_agent_id, _session_id), run in self._active_by_session.items():
            if active_agent_id == agent_id and run.status == RunStatus.RUNNING:
                return True
        return any(
            queued_agent_id == agent_id and bool(queue)
            for (queued_agent_id, _session_id), queue in self._queues.items()
        )

    async def _execute(
        self,
        run: Run,
        session_key: tuple[str, str],
        executor: RunExecutor,
    ) -> None:
        timing_started_at = datetime.now(UTC)
        timing_started_perf = time.perf_counter()

        def terminal_timing() -> JsonObject:
            completed_at = datetime.now(UTC)
            duration_ms = max(0, round((time.perf_counter() - timing_started_perf) * 1000))
            return {
                "started_at": timing_started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_ms": duration_ms,
            }

        try:
            started_payload: JsonObject = {"status": RunStatus.RUNNING.value}
            if run._started_from_queue_item_id is not None:  # noqa: SLF001 - executor shares run instance.
                started_payload["queue_item_id"] = run._started_from_queue_item_id  # noqa: SLF001
            run.emit(RUN_STARTED_EVENT, started_payload)
            result = await executor(run)
            if run.cancel_requested:
                run.mark_cancelled(payload_extras={"timing": terminal_timing()})
                return
            result_usage = getattr(result, "usage", None) if result is not None else None
            payload_extras: JsonObject = {"timing": terminal_timing()}
            if result_usage:
                payload_extras["usage"] = result_usage
            run.mark_completed(result, payload_extras=payload_extras)
        except asyncio.CancelledError:
            run.mark_cancelled(payload_extras={"timing": terminal_timing()})
        except BaseException as exc:
            if run.cancel_requested:
                run.mark_cancelled(payload_extras={"timing": terminal_timing()})
                return
            run.mark_failed(exc, payload_extras={"timing": terminal_timing()})
        finally:
            async with self._lock:
                if self._active_by_session.get(session_key) is run:
                    self._active_by_session.pop(session_key, None)
                self._prune_terminal_runs_locked()
            await self._drain_next(session_key)

    async def _drain_next(self, session_key: tuple[str, str]) -> None:
        async with self._lock:
            active_run = self._active_by_session.get(session_key)
            if active_run is not None and active_run.status == RunStatus.RUNNING:
                return

            queue = self._queues.get(session_key)
            if not queue:
                self._queues.pop(session_key, None)
                return

            item = queue.popleft()
            if not queue:
                self._queues.pop(session_key, None)

            agent_id, session_id = session_key
            run = self._start_run_locked(
                session_key=session_key,
                agent_id=agent_id,
                session_id=session_id,
                executor=item.executor,
                queue_item_id=item.item_id,
            )
            if not item.future.done():
                item.future.set_result(run)

    def _start_run_locked(
        self,
        *,
        session_key: tuple[str, str],
        agent_id: str,
        session_id: str,
        executor: RunExecutor,
        queue_item_id: str | None = None,
    ) -> Run:
        run = Run(
            run_id=str(uuid.uuid4()),
            agent_id=agent_id,
            session_id=session_id,
            event_retention_limit=self._run_event_retention_limit,
        )
        run._started_from_queue_item_id = queue_item_id  # noqa: SLF001 - run carries its own start origin.
        self._active_by_session[session_key] = run
        self._runs[run.id] = run
        task = asyncio.create_task(self._execute(run, session_key, executor))
        run.set_task(task)
        self._notify_run_started(run)
        return run

    def _notify_run_started(self, run: Run) -> None:
        for callback in list(self._run_started_callbacks):
            try:
                callback(run)
            except Exception:
                _LOGGER.warning("Run start callback failed", exc_info=True)

    def _prune_terminal_runs_locked(self) -> None:
        terminal_run_ids = [
            run_id for run_id, run in self._runs.items() if run.status != RunStatus.RUNNING
        ]
        overflow = len(terminal_run_ids) - self._completed_run_retention_limit
        for run_id in terminal_run_ids[: max(0, overflow)]:
            self._runs.pop(run_id, None)


def _drain_queue(queue: asyncio.Queue[Any]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _schedule_callback(callback: CancelCallback) -> None:
    try:
        result = callback()
    except Exception:
        _LOGGER.warning("Run cancel callback failed", exc_info=True)
        return
    if inspect.isawaitable(result):
        task = asyncio.create_task(cast(Coroutine[Any, Any, Any], result))
        task.add_done_callback(_on_cancel_callback_done)


def _on_cancel_callback_done(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        _LOGGER.warning("Run async cancel callback failed", exc_info=True)
