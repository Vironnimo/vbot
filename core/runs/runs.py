"""Run coordination primitives for session execution."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from core.event_stream import ReplayEventStream
from core.utils.errors import VBotError

JsonObject = dict[str, Any]
RunExecutor = Callable[["Run"], Awaitable[Any]]
CancelCallback = Callable[[], Any]
# Session identity used to key active runs and queues:
# ``(project_id, agent_id, session_id)`` — the project anchor is part of the key
# because ``session.create`` accepts caller-chosen session ids, so identity
# ``builder`` and project ``builder@vbot`` may both own a session named ``main``
# and must never block/cancel/guard each other. ``project_id`` is ``None`` for an
# identity session. Every lookup method takes an explicit required ``project_id``
# so no caller can silently fall into the identity scope.
SessionKey = tuple[str | None, str, str]
_LOGGER = logging.getLogger("vbot.runs")
DEFAULT_RUN_EVENT_RETENTION_LIMIT = 4096
DEFAULT_RUN_SUBSCRIBER_QUEUE_LIMIT = 4096
DEFAULT_COMPLETED_RUN_RETENTION_LIMIT = 512
DEFAULT_WAITING_WORK_LIMIT = 32

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
MODEL_STEP_USAGE_EVENT = "model_step_usage"
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


class RunAdmissionBlockedError(ActiveRunError):
    """Raised when Run activity and a lifecycle admission guard conflict."""


class RunNotFoundError(RunError):
    """Raised when a run id is unknown."""


class RunCancelledError(RunError):
    """Raised when awaiting a cancelled run."""


class WaitingWorkLimitError(RunError):
    """Raised when accepting more waiting work would exceed a queue limit."""


@dataclass(frozen=True, slots=True)
class WaitingWorkAdmission:
    """One reserved waiting-work slot held before a Run can be enqueued.

    Channel ingress obtains this reservation before downloading media. The
    reservation is either released once work begins or atomically transferred
    to a queued Run, so the shared manager remains the authority for all
    waiting-work capacity.
    """

    id: str
    scope: str


@dataclass
class QueuedRunItem:
    """One queued run request waiting for a session turn slot."""

    item_id: str
    display_content: str
    executor: RunExecutor
    internal: bool
    future: asyncio.Future[Run]
    working_project_id: str | None = field(default=None, repr=False)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    waiting_scope: str | None = field(default=None, repr=False)

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
    # The project anchor the emitting run executes under (``None`` for an
    # identity run). ``agent_id`` stays bare; the project rides as a sibling
    # field so a consumer can rebuild the outside ``agent@projekt`` address.
    project_id: str | None = None
    payload: JsonObject = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> JsonObject:
        """Return a JSON-compatible event dictionary."""
        return {
            "sequence": self.sequence,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "type": self.type,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
        }


class _CancelledToolCallSentinel:
    """Internal marker that a per-tool-call cancel was already invoked."""


_CANCELLED_TOOL_CALL = _CancelledToolCallSentinel()


class Run:
    """One active execution inside a persisted chat session."""

    def __init__(
        self,
        *,
        run_id: str,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        working_project_id: str | None = None,
        event_retention_limit: int = DEFAULT_RUN_EVENT_RETENTION_LIMIT,
        subscriber_queue_limit: int = DEFAULT_RUN_SUBSCRIBER_QUEUE_LIMIT,
    ) -> None:
        self.id = run_id
        self.agent_id = agent_id
        self.session_id = session_id
        # The project anchor the run executes under (``None`` for an identity
        # run). Carried solely so the executor's session I/O finds the
        # project-scoped transcript path — it is not part of the run/queue key.
        self.project_id = project_id
        # Internal working context. This never participates in Session identity,
        # public addressing, events, or queue keys.
        self.working_project_id = working_project_id
        self.status = RunStatus.RUNNING
        self.created_at = datetime.now(UTC).isoformat()
        self.updated_at = self.created_at
        self.result: Any | None = None
        self.error: BaseException | None = None
        self.cancel_requested = False
        self.cancel_reason: str | None = None
        self._next_sequence = 1
        self._event_stream = ReplayEventStream[RunEvent](
            event_retention_limit=event_retention_limit,
            subscriber_queue_limit=subscriber_queue_limit,
            sequence_of=lambda event: event.sequence,
            terminal_when=lambda event: event.type in TERMINAL_EVENT_TYPES,
            on_lagged=lambda: _LOGGER.warning("Evicted lagging run subscriber for run %s", self.id),
        )
        self._done = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # ``Task.cancel()`` before a newly created task gets its first event-loop
        # step bypasses the coroutine's try/finally entirely. Keep the task alive
        # for that one step so the manager can mark the Run terminal and release
        # the session; once execution has entered, normal forceful cancellation
        # remains unchanged.
        self._execution_started = False
        self._cancel_callbacks: list[CancelCallback] = []
        self._tool_cancel_callbacks: dict[str, CancelCallback | _CancelledToolCallSentinel] = {}
        self._started_from_queue_item_id: str | None = None
        # Executor-supplied extras merged into every terminal event payload
        # (e.g. the chat loop's end-of-run session usage totals). Filled by the
        # executor before it returns/raises; the manager merges it alongside
        # ``timing`` regardless of the terminal outcome.
        self.terminal_payload_extras: JsonObject = {}
        # Log-facing run telemetry, incremented by the chat loop as the run
        # progresses and read by the end-of-run log line. Token totals sum the
        # per-turn usage payloads (estimated turns included — the log line cares
        # about magnitude; the statistics domain owns real-vs-estimated rigor).
        self.model_step_count = 0
        self.tool_call_count = 0
        self.input_token_total = 0
        self.output_token_total = 0

    @property
    def events(self) -> list[RunEvent]:
        """Return a replayable snapshot of events emitted so far."""
        return self._event_stream.events

    @property
    def subscriber_count(self) -> int:
        """Return the number of active live event subscribers."""

        return self._event_stream.subscriber_count

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
        if self._task is not None and self._execution_started:
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

    def emit(
        self,
        event_type: str,
        payload: JsonObject | None = None,
        *,
        allow_after_cancel: bool = False,
    ) -> RunEvent | None:
        """Append and publish one visible run event.

        After cancellation is requested, only terminal events are forwarded. This
        keeps late provider/tool results from becoming visible. The one deliberate
        escape is ``allow_after_cancel``: an executor may still publish an event
        that *finalizes output the user has already seen* (the chat loop's
        preserved partial answer on cancel) — never new or late results.
        """
        if self.status != RunStatus.RUNNING and event_type not in TERMINAL_EVENT_TYPES:
            return None
        if (
            self.cancel_requested
            and event_type not in TERMINAL_EVENT_TYPES
            and not allow_after_cancel
        ):
            return None
        event = RunEvent(
            sequence=self._next_sequence,
            run_id=self.id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            project_id=self.project_id,
            type=event_type,
            payload=dict(payload or {}),
        )
        self._next_sequence += 1
        self._event_stream.publish(event)
        self.updated_at = event.timestamp
        return event

    async def subscribe(self, *, after_sequence: int = 0) -> AsyncGenerator[RunEvent, None]:
        """Replay old events and stream future events until a terminal event."""
        async with aclosing(
            self._event_stream.subscribe(
                after_sequence=after_sequence,
                live=self.status == RunStatus.RUNNING,
            )
        ) as events:
            async for event in events:
                yield event

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
        waiting_work_limit: int = DEFAULT_WAITING_WORK_LIMIT,
    ) -> None:
        if completed_run_retention_limit < 1:
            raise ValueError("completed_run_retention_limit must be positive")
        if run_event_retention_limit < 1:
            raise ValueError("run_event_retention_limit must be positive")
        if waiting_work_limit < 1:
            raise ValueError("waiting_work_limit must be positive")
        self._lock = asyncio.Lock()
        self._active_by_session: dict[SessionKey, Run] = {}
        self._queues: dict[SessionKey, deque[QueuedRunItem]] = {}
        self._guarded_sessions: set[SessionKey] = set()
        self._guarded_agents: set[tuple[str | None, str]] = set()
        self._guarded_projects: set[str] = set()
        self._waiting_work_admissions: dict[str, WaitingWorkAdmission] = {}
        self._runs: dict[str, Run] = {}
        self._run_started_callbacks: list[Callable[[Run], None]] = []
        self._completed_run_retention_limit = completed_run_retention_limit
        self._run_event_retention_limit = run_event_retention_limit
        self._waiting_work_limit = waiting_work_limit

    def reserve_waiting_work(
        self,
        *,
        scope: str,
        scope_limit: int,
    ) -> WaitingWorkAdmission:
        """Reserve one waiting-work slot before expensive ingress processing.

        Reservations cover work that has been accepted by an ingress path but
        cannot yet become a queued Run, for example a channel attachment that
        must not be downloaded until capacity is known. A reservation later
        moves atomically into :meth:`enqueue` or is released when processing
        starts without creating a Run.
        """
        if not scope:
            raise ValueError("waiting work scope must not be empty")
        if scope_limit < 1:
            raise ValueError("waiting work scope_limit must be positive")

        waiting_count = self._waiting_work_count()
        if waiting_count >= self._waiting_work_limit:
            _LOGGER.warning(
                "Waiting work rejected by global limit (scope=%s waiting=%d limit=%d)",
                scope,
                waiting_count,
                self._waiting_work_limit,
            )
            raise WaitingWorkLimitError("global waiting work limit reached")

        scoped_waiting_count = self._waiting_work_count_for_scope(scope)
        if scoped_waiting_count >= scope_limit:
            _LOGGER.warning(
                "Waiting work rejected by scope limit (scope=%s waiting=%d limit=%d)",
                scope,
                scoped_waiting_count,
                scope_limit,
            )
            raise WaitingWorkLimitError("waiting work scope limit reached")

        admission = WaitingWorkAdmission(id=str(uuid.uuid4()), scope=scope)
        self._waiting_work_admissions[admission.id] = admission
        return admission

    def release_waiting_work(self, admission: WaitingWorkAdmission) -> bool:
        """Release an unused ingress reservation, returning whether it was held."""
        current = self._waiting_work_admissions.get(admission.id)
        if current != admission:
            return False
        self._waiting_work_admissions.pop(admission.id)
        return True

    def waiting_work_count(self) -> int:
        """Return the system-wide number of tasks waiting for processing."""
        return self._waiting_work_count()

    def add_run_started_callback(self, callback: Callable[[Run], None]) -> Callable[[], None]:
        """Register a callback invoked whenever this manager starts a Run."""
        self._run_started_callbacks.append(callback)

        def remove_callback() -> None:
            if callback in self._run_started_callbacks:
                self._run_started_callbacks.remove(callback)

        return remove_callback

    @asynccontextmanager
    async def session_admission_guard(self, *session_keys: SessionKey) -> AsyncIterator[None]:
        """Hold one atomic no-Run boundary across a Session transition.

        Every supplied Session must be idle when the guard is acquired. While
        held, both immediate starts and queued admission are rejected. Supplying
        source and destination keys together protects an Agent Takeover through
        the destination divider/note writes as one transition.
        """
        guarded_sessions = frozenset(session_keys)
        if not guarded_sessions:
            raise ValueError("session admission guard requires at least one session")

        async with self._lock:
            if any(
                self._has_activity_for_session_locked(session_key)
                or self._run_admission_is_guarded_locked(session_key, working_project_id=None)
                for session_key in guarded_sessions
            ):
                raise RunAdmissionBlockedError(
                    "session transition conflicts with active, queued, or guarded work"
                )
            self._guarded_sessions.update(guarded_sessions)
        try:
            yield
        finally:
            await asyncio.shield(self._release_session_admission_guard(guarded_sessions))

    @asynccontextmanager
    async def agent_admission_guard(
        self, agent_id: str, *, project_id: str | None
    ) -> AsyncIterator[None]:
        """Hold one atomic no-Run boundary across an Agent removal."""
        agent_key = (project_id, agent_id)
        async with self._lock:
            conflicts_with_guard = (
                agent_key in self._guarded_agents
                or any(
                    guarded_project_id == project_id and guarded_agent_id == agent_id
                    for guarded_project_id, guarded_agent_id, _session_id in self._guarded_sessions
                )
                or (project_id is not None and project_id in self._guarded_projects)
            )
            if conflicts_with_guard or self._has_activity_for_agent_locked(agent_key):
                raise RunAdmissionBlockedError(
                    "agent removal conflicts with active, queued, or guarded work"
                )
            self._guarded_agents.add(agent_key)
        try:
            yield
        finally:
            await asyncio.shield(self._release_agent_admission_guard(agent_key))

    @asynccontextmanager
    async def project_admission_guard(self, project_id: str) -> AsyncIterator[None]:
        """Hold one atomic no-Run boundary across a Project removal.

        The boundary covers both Project-owned Sessions and Identity-Agent work
        whose internal working Project is the removed Project.
        """
        async with self._lock:
            conflicts_with_guard = (
                project_id in self._guarded_projects
                or any(
                    guarded_project_id == project_id
                    for guarded_project_id, _agent_id, _session_id in self._guarded_sessions
                )
                or any(
                    guarded_project_id == project_id
                    for guarded_project_id, _agent_id in self._guarded_agents
                )
            )
            if conflicts_with_guard or self._has_activity_for_project_locked(project_id):
                raise RunAdmissionBlockedError(
                    "project removal conflicts with active, queued, or guarded work"
                )
            self._guarded_projects.add(project_id)
        try:
            yield
        finally:
            await asyncio.shield(self._release_project_admission_guard(project_id))

    async def start(
        self,
        *,
        agent_id: str,
        session_id: str,
        executor: RunExecutor,
        project_id: str | None,
        working_project_id: str | None = None,
    ) -> Run:
        """Start one run if the session has no active run.

        ``project_id`` is a required part of the session key (see ``SessionKey``)
        and also rides onto the created ``Run`` so the executor's session I/O
        finds the project-scoped path. ``None`` names the identity anchor.
        """
        session_key = (project_id, agent_id, session_id)
        async with self._lock:
            self._ensure_run_admission_allowed_locked(session_key, working_project_id)
            active_run = self._active_by_session.get(session_key)
            if active_run is not None and active_run.status == RunStatus.RUNNING:
                raise ActiveRunError(f"session already has an active run: {session_id}")
            return self._start_run_locked(
                session_key=session_key,
                executor=executor,
                working_project_id=working_project_id,
            )

    async def enqueue(
        self,
        *,
        agent_id: str,
        session_id: str,
        executor: RunExecutor,
        display_content: str = "",
        internal: bool = False,
        project_id: str | None,
        working_project_id: str | None = None,
        waiting_work_admission: WaitingWorkAdmission | None = None,
    ) -> QueuedRunItem:
        """Start immediately when idle or append one item to the session queue."""
        session_key = (project_id, agent_id, session_id)
        future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        item = QueuedRunItem(
            item_id=str(uuid.uuid4()),
            display_content=display_content,
            executor=executor,
            internal=internal,
            future=future,
            working_project_id=working_project_id,
        )

        def remove_abandoned_item(completed_future: asyncio.Future[Run]) -> None:
            # Awaiting a bare Future propagates task cancellation into that Future.
            # The Future has one owner, so cancellation means the accepted work was
            # abandoned and must not remain queued to execute without a consumer.
            if completed_future.cancelled():
                self.remove_queued(
                    agent_id,
                    session_id,
                    item.item_id,
                    project_id=project_id,
                )

        item.future.add_done_callback(remove_abandoned_item)

        async with self._lock:
            try:
                self._ensure_run_admission_allowed_locked(session_key, working_project_id)
            except RunAdmissionBlockedError:
                item.future.cancel()
                raise
            active_run = self._active_by_session.get(session_key)
            if active_run is None or active_run.status != RunStatus.RUNNING:
                self._consume_waiting_work_admission(waiting_work_admission)
                run = self._start_run_locked(
                    session_key=session_key,
                    executor=item.executor,
                    working_project_id=item.working_project_id,
                    queue_item_id=item.item_id,
                )
                item.future.set_result(run)
                return item

            waiting_scope = self._consume_waiting_work_admission(waiting_work_admission)
            if waiting_scope is None and self._waiting_work_count() >= self._waiting_work_limit:
                _LOGGER.warning(
                    "Run rejected by global waiting-work limit (agent=%s session=%s limit=%d)",
                    agent_id,
                    session_id,
                    self._waiting_work_limit,
                )
                raise WaitingWorkLimitError("global waiting work limit reached")

            item.waiting_scope = waiting_scope
            queue = self._queues.setdefault(session_key, deque())
            queue.append(item)
            _LOGGER.info(
                "Run queued for busy session (agent=%s session=%s queue_depth=%d)",
                agent_id,
                session_id,
                len(queue),
            )
            return item

    def _consume_waiting_work_admission(self, admission: WaitingWorkAdmission | None) -> str | None:
        """Remove one held reservation and return its scope for a queued Run."""
        if admission is None:
            return None
        current = self._waiting_work_admissions.get(admission.id)
        if current != admission:
            raise ValueError("waiting work admission is no longer active")
        self._waiting_work_admissions.pop(admission.id)
        return admission.scope

    def _waiting_work_count(self) -> int:
        return len(self._waiting_work_admissions) + sum(
            len(queue) for queue in self._queues.values()
        )

    def _waiting_work_count_for_scope(self, scope: str) -> int:
        return sum(
            admission.scope == scope for admission in self._waiting_work_admissions.values()
        ) + sum(item.waiting_scope == scope for queue in self._queues.values() for item in queue)

    def list_queued(
        self, agent_id: str, session_id: str, *, project_id: str | None
    ) -> list[QueuedRunItem]:
        """Return queued items for one session in FIFO order."""
        return list(self._queues.get((project_id, agent_id, session_id), ()))

    def all_queued(self) -> list[tuple[SessionKey, QueuedRunItem]]:
        """Return a fresh snapshot of queued items across every session."""
        return [
            (session_key, item) for session_key, queue in self._queues.items() for item in queue
        ]

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None
    ) -> bool:
        """Remove one queued item if present."""
        session_key = (project_id, agent_id, session_id)
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
        *,
        project_id: str | None,
    ) -> bool:
        """Replace the queued executor and display text for one item."""
        queue = self._queues.get((project_id, agent_id, session_id))
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

    def cancel_by_session(
        self,
        agent_id: str,
        session_id: str,
        *,
        project_id: str | None,
        reason: str | None = None,
    ) -> Run:
        """Request cancellation for the active run in one session."""
        run = self._active_by_session.get((project_id, agent_id, session_id))
        if run is None or run.status != RunStatus.RUNNING:
            raise RunNotFoundError(f"no active run for agent '{agent_id}' session '{session_id}'")
        run.request_cancel(reason=reason)
        return run

    def active_run(self, *, agent_id: str, session_id: str, project_id: str | None) -> Run | None:
        """Return the active run for a session, if one exists."""
        run = self._active_by_session.get((project_id, agent_id, session_id))
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

    def has_activity_for_agent(self, agent_id: str, *, project_id: str | None) -> bool:
        """Return whether an agent owns any running run or queued run item.

        The check spans every session of the ``(project_id, agent_id)`` pair —
        one agent in one anchor scope. Scoping by project keeps same-named agents
        apart: an active run of identity ``builder`` must not block removing an
        unrelated project whose team also has a ``builder``, and vice versa.
        """
        return self._has_activity_for_agent_locked((project_id, agent_id))

    def has_activity_for_working_project(self, project_id: str) -> bool:
        """Return whether active or queued work depends on one Project."""
        return self._has_activity_for_working_project_locked(project_id)

    def has_activity_for_session(
        self, agent_id: str, session_id: str, *, project_id: str | None
    ) -> bool:
        """Return a snapshot of whether one Session owns active or queued work.

        The session-scoped counterpart to :meth:`has_activity_for_agent`, keyed
        on the exact ``(project_id, agent_id, session_id)`` triple both the
        active-run and the queue maps use. Destructive lifecycle workflows use
        :meth:`session_admission_guard` instead because this snapshot alone
        cannot prevent a new Run from entering after the check.
        """
        return self._has_activity_for_session_locked((project_id, agent_id, session_id))

    def _ensure_run_admission_allowed_locked(
        self, session_key: SessionKey, working_project_id: str | None
    ) -> None:
        if self._run_admission_is_guarded_locked(session_key, working_project_id):
            raise RunAdmissionBlockedError(
                "run admission is blocked while its Session, Agent, or Project is transitioning"
            )

    def _run_admission_is_guarded_locked(
        self, session_key: SessionKey, working_project_id: str | None
    ) -> bool:
        project_id, agent_id, _session_id = session_key
        return (
            session_key in self._guarded_sessions
            or (project_id, agent_id) in self._guarded_agents
            or (project_id is not None and project_id in self._guarded_projects)
            or (working_project_id is not None and working_project_id in self._guarded_projects)
        )

    def _has_activity_for_session_locked(self, session_key: SessionKey) -> bool:
        active_run = self._active_by_session.get(session_key)
        if active_run is not None and active_run.status == RunStatus.RUNNING:
            return True
        return bool(self._queues.get(session_key))

    def _has_activity_for_agent_locked(self, agent_key: tuple[str | None, str]) -> bool:
        project_id, agent_id = agent_key
        if any(
            active_project_id == project_id
            and active_agent_id == agent_id
            and run.status == RunStatus.RUNNING
            for (active_project_id, active_agent_id, _session_id), run in (
                self._active_by_session.items()
            )
        ):
            return True
        return any(
            queued_project_id == project_id and queued_agent_id == agent_id and bool(queue)
            for (queued_project_id, queued_agent_id, _session_id), queue in self._queues.items()
        )

    def _has_activity_for_working_project_locked(self, project_id: str) -> bool:
        if any(
            run.status == RunStatus.RUNNING and run.working_project_id == project_id
            for run in self._active_by_session.values()
        ):
            return True
        return any(
            item.working_project_id == project_id
            for queue in self._queues.values()
            for item in queue
        )

    def _has_activity_for_project_locked(self, project_id: str) -> bool:
        if self._has_activity_for_working_project_locked(project_id):
            return True
        if any(
            active_project_id == project_id and run.status == RunStatus.RUNNING
            for (active_project_id, _agent_id, _session_id), run in (
                self._active_by_session.items()
            )
        ):
            return True
        return any(
            queued_project_id == project_id and bool(queue)
            for (queued_project_id, _agent_id, _session_id), queue in self._queues.items()
        )

    async def _release_session_admission_guard(
        self, guarded_sessions: frozenset[SessionKey]
    ) -> None:
        async with self._lock:
            self._guarded_sessions.difference_update(guarded_sessions)

    async def _release_agent_admission_guard(self, agent_key: tuple[str | None, str]) -> None:
        async with self._lock:
            self._guarded_agents.remove(agent_key)

    async def _release_project_admission_guard(self, project_id: str) -> None:
        async with self._lock:
            self._guarded_projects.remove(project_id)

    async def _execute(
        self,
        run: Run,
        session_key: SessionKey,
        executor: RunExecutor,
    ) -> None:
        # This synchronous first step closes the create-task/immediate-cancel
        # race. A cancellation requested before entry deliberately did not cancel
        # the task; the check inside the try below then performs normal terminal
        # bookkeeping without ever entering the caller's executor.
        run._execution_started = True  # noqa: SLF001 - manager owns run lifecycle internals.
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

        def terminal_extras() -> JsonObject:
            extras: JsonObject = dict(run.terminal_payload_extras)
            extras["timing"] = terminal_timing()
            return extras

        try:
            run.raise_if_cancelled()
            started_payload: JsonObject = {"status": RunStatus.RUNNING.value}
            if run._started_from_queue_item_id is not None:  # noqa: SLF001 - executor shares run instance.
                started_payload["queue_item_id"] = run._started_from_queue_item_id  # noqa: SLF001
            run.emit(RUN_STARTED_EVENT, started_payload)
            result = await executor(run)
            if run.cancel_requested:
                run.mark_cancelled(payload_extras=terminal_extras())
                return
            result_usage = getattr(result, "usage", None) if result is not None else None
            payload_extras: JsonObject = terminal_extras()
            if result_usage:
                payload_extras["usage"] = result_usage
            run.mark_completed(result, payload_extras=payload_extras)
        except asyncio.CancelledError:
            run.mark_cancelled(payload_extras=terminal_extras())
        except (KeyboardInterrupt, SystemExit):
            # Process-level interrupts must never be downgraded to a failed run:
            # record the run as cancelled best-effort, then let the interrupt
            # propagate so shutdown proceeds. Other non-Exception BaseExceptions
            # (e.g. GeneratorExit) likewise fall through untouched.
            run.mark_cancelled(payload_extras=terminal_extras())
            raise
        except Exception as exc:
            if run.cancel_requested:
                run.mark_cancelled(payload_extras=terminal_extras())
                return
            run.mark_failed(exc, payload_extras=terminal_extras())
        finally:
            async with self._lock:
                if self._active_by_session.get(session_key) is run:
                    self._active_by_session.pop(session_key, None)
                self._prune_terminal_runs_locked()
            await self._drain_next(session_key)

    async def _drain_next(self, session_key: SessionKey) -> None:
        async with self._lock:
            active_run = self._active_by_session.get(session_key)
            if active_run is not None and active_run.status == RunStatus.RUNNING:
                return

            queue = self._queues.get(session_key)
            if not queue:
                self._queues.pop(session_key, None)
                return

            while queue:
                item = queue.popleft()
                # The cancellation callback normally removes an abandoned item
                # immediately. This guard closes the same-tick race where the
                # active Run drains before that callback gets its event-loop turn.
                if item.future.done():
                    continue
                if not queue:
                    self._queues.pop(session_key, None)

                run = self._start_run_locked(
                    session_key=session_key,
                    executor=item.executor,
                    queue_item_id=item.item_id,
                    working_project_id=item.working_project_id,
                )
                item.future.set_result(run)
                return

            self._queues.pop(session_key, None)

    def _start_run_locked(
        self,
        *,
        session_key: SessionKey,
        executor: RunExecutor,
        queue_item_id: str | None = None,
        working_project_id: str | None = None,
    ) -> Run:
        # The key is the single source of the run's identity: the project anchor,
        # agent, and session all come from it, so a drained queue item can never
        # start under a different anchor than it was enqueued for.
        project_id, agent_id, session_id = session_key
        run = Run(
            run_id=str(uuid.uuid4()),
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            working_project_id=working_project_id,
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
