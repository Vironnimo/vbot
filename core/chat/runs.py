"""Run coordination primitives for chat execution."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from core.utils.errors import VBotError

JsonObject = dict[str, Any]
RunExecutor = Callable[["Run"], Awaitable[Any]]
CancelCallback = Callable[[], Any]

RUN_STARTED_EVENT = "run_started"
USER_MESSAGE_EVENT = "user_message_persisted"
REASONING_EVENT = "reasoning"
ASSISTANT_OUTPUT_DELTA_EVENT = "assistant_output_delta"
REASONING_DELTA_EVENT = "reasoning_delta"
TOOL_CALL_DELTA_EVENT = "tool_call_delta"
TOOL_CALL_STARTED_EVENT = "tool_call_started"
TOOL_CALL_RESULT_EVENT = "tool_call_result"
ASSISTANT_OUTPUT_EVENT = "assistant_output"
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


class Run:
    """One active execution inside a persisted chat session."""

    def __init__(self, *, run_id: str, agent_id: str, session_id: str) -> None:
        self.id = run_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.status = RunStatus.RUNNING
        self.created_at = datetime.now(UTC).isoformat()
        self.updated_at = self.created_at
        self.result: Any | None = None
        self.error: BaseException | None = None
        self.cancel_requested = False
        self._events: list[RunEvent] = []
        self._subscribers: list[asyncio.Queue[RunEvent]] = []
        self._done = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._cancel_callbacks: list[CancelCallback] = []

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

    def request_cancel(self) -> None:
        """Request best-effort cancellation of this run."""
        if self.status != RunStatus.RUNNING or self.cancel_requested:
            return
        self.cancel_requested = True
        for callback in list(self._cancel_callbacks):
            _schedule_callback(callback)
        if self._task is not None:
            self._task.cancel()

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
            sequence=len(self._events) + 1,
            run_id=self.id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            type=event_type,
            payload=dict(payload or {}),
        )
        self._events.append(event)
        self.updated_at = event.timestamp
        for subscriber in list(self._subscribers):
            subscriber.put_nowait(event)
        return event

    async def subscribe(self, *, after_sequence: int = 0) -> AsyncIterator[RunEvent]:
        """Replay old events and stream future events until a terminal event."""
        for event in self._events:
            if event.sequence > after_sequence:
                yield event
                if event.type in TERMINAL_EVENT_TYPES:
                    return

        if self.status != RunStatus.RUNNING:
            return

        queue: asyncio.Queue[RunEvent] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                if event.sequence <= after_sequence:
                    continue
                yield event
                if event.type in TERMINAL_EVENT_TYPES:
                    return
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

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
        """Move the run to failed and publish the terminal event."""
        if self.status != RunStatus.RUNNING:
            return
        self.error = error
        self.status = RunStatus.FAILED
        payload: JsonObject = {"status": self.status.value, "error": str(error)}
        if payload_extras:
            payload.update(payload_extras)
        self.emit(RUN_FAILED_EVENT, payload)
        self._done.set()

    def mark_cancelled(self) -> None:
        """Move the run to cancelled and publish the terminal event."""
        if self.status != RunStatus.RUNNING:
            return
        self.status = RunStatus.CANCELLED
        self.emit(RUN_CANCELLED_EVENT, {"status": self.status.value})
        self._done.set()


class ChatRunManager:
    """Coordinates active chat runs across sessions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active_by_session: dict[tuple[str, str], Run] = {}
        self._runs: dict[str, Run] = {}

    async def start(self, *, agent_id: str, session_id: str, executor: RunExecutor) -> Run:
        """Start one run if the session has no active run."""
        session_key = (agent_id, session_id)
        async with self._lock:
            active_run = self._active_by_session.get(session_key)
            if active_run is not None and active_run.status == RunStatus.RUNNING:
                raise ActiveRunError(f"session already has an active run: {session_id}")
            run = Run(run_id=str(uuid.uuid4()), agent_id=agent_id, session_id=session_id)
            self._active_by_session[session_key] = run
            self._runs[run.id] = run

        task = asyncio.create_task(self._execute(run, session_key, executor))
        run.set_task(task)
        return run

    def get(self, run_id: str) -> Run:
        """Return a run by id."""
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"run not found: {run_id}") from exc

    async def cancel(self, run_id: str) -> Run:
        """Request cancellation and wait until the run reaches a terminal state."""
        run = self.get(run_id)
        run.request_cancel()
        await run._done.wait()  # noqa: SLF001 - manager owns run lifecycle internals.
        return run

    def active_run(self, *, agent_id: str, session_id: str) -> Run | None:
        """Return the active run for a session, if one exists."""
        run = self._active_by_session.get((agent_id, session_id))
        if run is None or run.status != RunStatus.RUNNING:
            return None
        return run

    async def _execute(
        self,
        run: Run,
        session_key: tuple[str, str],
        executor: RunExecutor,
    ) -> None:
        try:
            run.emit(RUN_STARTED_EVENT, {"status": RunStatus.RUNNING.value})
            result = await executor(run)
            if run.cancel_requested:
                run.mark_cancelled()
                return
            result_usage = getattr(result, "usage", None) if result is not None else None
            payload_extras = {"usage": result_usage} if result_usage else None
            run.mark_completed(result, payload_extras=payload_extras)
        except asyncio.CancelledError:
            run.mark_cancelled()
        except BaseException as exc:
            if run.cancel_requested:
                run.mark_cancelled()
                return
            run.mark_failed(exc)
        finally:
            async with self._lock:
                if self._active_by_session.get(session_key) is run:
                    self._active_by_session.pop(session_key, None)


def _schedule_callback(callback: CancelCallback) -> None:
    result = callback()
    if inspect.isawaitable(result):
        asyncio.create_task(cast(Coroutine[Any, Any, Any], result))
