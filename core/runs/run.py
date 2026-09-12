"""One Run: events, cancellation, execution records and terminal state."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from core.event_stream import ReplayEventStream
from core.utils.errors import VBotError

JsonObject = dict[str, Any]
RunExecutor = Callable[["Run"], Awaitable[Any]]
CancelCallback = Callable[[], Any]
# Active runs, queues, and guards key on ``SessionAddress`` (born in
# ``core.sessions``): the project anchor is part of the identity because
# ``session.create`` accepts caller-chosen session ids, so identity ``builder``
# and project ``builder@vbot`` may both own a session named ``main`` and must
# never block/cancel/guard each other. ``project_id`` is ``None`` for an
# identity session. Public lookup methods take an explicit required
# ``project_id`` so no caller can silently fall into the identity scope.
_LOGGER = logging.getLogger("vbot.runs")
DEFAULT_RUN_EVENT_RETENTION_LIMIT = 4096
DEFAULT_RUN_SUBSCRIBER_QUEUE_LIMIT = 4096
DEFAULT_COMPLETED_RUN_RETENTION_LIMIT = 512
DEFAULT_WAITING_WORK_LIMIT = 32
_CANCEL_CLEANUP_TIMEOUT_SECONDS = 5.0

RUN_STARTED_EVENT = "run_started"
USER_MESSAGE_EVENT = "user_message_persisted"
COMPACTION_STARTED_EVENT = "compaction_started"
COMPACTION_ABORTED_EVENT = "compaction_aborted"
COMPACTION_COMPLETED_EVENT = "compaction_completed"
REASONING_EVENT = "reasoning"
ASSISTANT_OUTPUT_DELTA_EVENT = "assistant_output_delta"
REASONING_DELTA_EVENT = "reasoning_delta"
TOOL_CALL_DELTA_EVENT = "tool_call_delta"
STREAM_ATTEMPT_RESTARTED_EVENT = "stream_attempt_restarted"
TOOL_CALL_STDOUT_EVENT = "tool_call_stdout"
TOOL_CALL_STDERR_EVENT = "tool_call_stderr"
TOOL_CALL_STARTED_EVENT = "tool_call_started"
TOOL_CALL_RESULT_EVENT = "tool_call_result"
ASSISTANT_OUTPUT_EVENT = "assistant_output"
ERROR_MESSAGE_PERSISTED_EVENT = "error_message_persisted"
MODEL_FALLBACK_ACTIVATED_EVENT = "model_fallback_activated"
MODEL_STEP_USAGE_EVENT = "model_step_usage"
RUN_CHANGE_STATS_EVENT = "run_change_stats"
PROVIDER_HEARTBEAT_EVENT = "provider_heartbeat"
RUN_COMPLETED_EVENT = "run_completed"
RUN_FAILED_EVENT = "run_failed"
RUN_CANCELLED_EVENT = "run_cancelled"
RUN_INTERRUPTED_EVENT = "run_interrupted"
TERMINAL_EVENT_TYPES = {
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_INTERRUPTED_EVENT,
}
RUN_AGENT_ACTIVITY_FIELD = "contributes_to_agent_activity"
RUN_KIND_FIELD = "run_kind"


class RunStatus(StrEnum):
    """Terminal and active states for a chat run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunKind(StrEnum):
    """Stable origin category for one admitted Run."""

    USER = "user"
    CHANNEL = "channel"
    CRON = "cron"
    CALENDAR = "calendar"
    REFLECTION = "reflection"
    MEMORY_REFLECTION = "memory_reflection"
    SKILL_REFLECTION = "skill_reflection"
    SUBAGENT = "subagent"
    SYSTEM = "system"


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


class RunInterruptedError(RunError):
    """Signal that bounded automatic recovery could not finish a Run."""

    def __init__(self, cause: str, *, result: Any | None = None) -> None:
        super().__init__(f"run interrupted: {cause}")
        self.cause = cause
        self.result = result


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


@dataclass(frozen=True, slots=True)
class RunExecutionOwner:
    """Immutable Extension-owned execution identity carried by a Run."""

    extension: str
    group_id: str
    participant_id: str
    generation_id: str
    epoch: str


@dataclass(frozen=True, slots=True)
class RunAdmission:
    """Immutable admission decisions carried with one inbound Run request.

    Every :meth:`ChatRunManager.start` / :meth:`ChatRunManager.enqueue` call
    supplies these once as one bundle instead of repeating four kwargs. The
    values are stored unchanged on the created ``Run`` (or its queued item) and
    never influence Run admission, execution, or cancellation.
    """

    working_project_id: str | None = None
    run_kind: RunKind = RunKind.USER
    contributes_to_agent_activity: bool = True
    work_id: str | None = None
    owner: RunExecutionOwner | None = None
    input_id: str | None = None


# Module-level singleton so ``admission`` can default without a call at the
# signature (ruff B008); the instance is immutable.
DEFAULT_RUN_ADMISSION = RunAdmission()


@dataclass
class QueuedRunItem:
    """One queued run request waiting for a session turn slot."""

    item_id: str
    display_content: str
    executor: RunExecutor
    internal: bool
    future: asyncio.Future[Run]
    editable: bool = False
    admission: RunAdmission = field(default_factory=RunAdmission, repr=False)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    waiting_scope: str | None = field(default=None, repr=False)

    def to_dict(self) -> JsonObject:
        """Return a server-safe queued item dictionary."""
        return {
            "id": self.item_id,
            "content": self.display_content,
            "editable": self.editable,
            "internal": self.internal,
            RUN_KIND_FIELD: self.admission.run_kind.value,
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
    run_kind: RunKind = RunKind.USER
    contributes_to_agent_activity: bool = True
    payload: JsonObject = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> JsonObject:
        """Return a JSON-compatible event dictionary."""
        data: JsonObject = {
            "sequence": self.sequence,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            RUN_KIND_FIELD: self.run_kind.value,
            "type": self.type,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
        }
        if not self.contributes_to_agent_activity:
            data[RUN_AGENT_ACTIVITY_FIELD] = False
        return data


class _CancelledToolCallSentinel:
    """Internal marker that a per-tool-call cancel was already invoked."""


_CANCELLED_TOOL_CALL = _CancelledToolCallSentinel()


class _ActiveToolCallSentinel:
    """Internal marker for a started call whose cancel callback is not ready yet."""


_ACTIVE_TOOL_CALL = _ActiveToolCallSentinel()


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
        run_kind: RunKind = RunKind.USER,
        contributes_to_agent_activity: bool = True,
        work_id: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
        execution_input_id: str | None = None,
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
        self.execution_owner = execution_owner
        self.execution_input_id = execution_input_id
        # Internal working context. This never participates in Session identity,
        # public addressing, events, or queue keys.
        self.working_project_id = working_project_id
        self.run_kind = run_kind
        # Accessors may exclude system work from Agent/Session status while the
        # Run remains fully executable, observable, and persisted.
        self.contributes_to_agent_activity = contributes_to_agent_activity
        # Stable public correlation for work whose durable result must remain
        # addressable after the in-memory Run has been pruned. It is internal
        # to Run orchestration and is persisted only on the terminal summary.
        self.work_id = work_id
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
        self._tool_cancel_callbacks: dict[
            str,
            CancelCallback | _ActiveToolCallSentinel | _CancelledToolCallSentinel,
        ] = {}
        self._cancel_cleanup_futures: set[asyncio.Future[Any]] = set()
        self._cancel_cleanup_expired = False
        self._started_from_queue_item_id: str | None = None
        # Executor-supplied extras merged into every terminal event payload
        # (e.g. the chat loop's end-of-run session usage totals). Filled by the
        # executor before it returns/raises; the manager merges it alongside
        # ``timing`` regardless of the terminal outcome.
        self.terminal_payload_extras: JsonObject = {}
        # Canonical Agentic Loop iteration count. Chat increments this only
        # after one Model request has returned a response; callers must never
        # reconstruct it from Assistant messages or Tool Calls. Token totals
        # sum the per-turn usage payloads (estimated turns included — the log
        # line cares about magnitude; Statistics owns real-vs-estimated rigor).
        self.iteration_count = 0
        self.tool_call_count = 0
        self.tool_call_names: set[str] = set()
        self.input_token_total = 0
        self.output_token_total = 0
        self.compaction_state = "unavailable"
        self._user_compaction_requested = False
        self._tool_background_callbacks: dict[str, Callable[[], bool]] = {}

    def controls(self) -> JsonObject:
        """Project live accessor controls; terminal Runs expose no actions."""
        active = self.status == RunStatus.RUNNING and not self.cancel_requested
        return {
            "compaction": self.compaction_state if active else "unavailable",
            "background_tool_call_ids": list(self._tool_background_callbacks) if active else [],
        }

    def set_compaction_state(self, state: str) -> None:
        if self.compaction_state != state:
            self.compaction_state = state
            self.emit("run_controls_changed", self.controls())

    def request_compaction(self) -> bool:
        if self.controls()["compaction"] not in {"idle", "pending", "running"}:
            return False
        if self.compaction_state == "idle":
            self._user_compaction_requested = True
            self.set_compaction_state("pending")
            _LOGGER.info("Compaction requested (run=%s session=%s)", self.id, self.session_id)
        return True

    def register_tool_background(self, tool_call_id: str, callback: Callable[[], bool]) -> None:
        if self.cancel_requested or self.tool_call_cancelled(tool_call_id):
            return
        self._tool_background_callbacks[tool_call_id] = callback
        self.emit("run_controls_changed", self.controls())

    def background_tool_call(self, tool_call_id: str) -> bool:
        if (
            self.status != RunStatus.RUNNING
            or self.cancel_requested
            or self.tool_call_cancelled(tool_call_id)
        ):
            return False
        callback = self._tool_background_callbacks.pop(tool_call_id, None)
        if callback is None:
            return False
        accepted = callback()
        self.emit("run_controls_changed", self.controls())
        if accepted:
            _LOGGER.info("Tool background requested (run=%s tool_call=%s)", self.id, tool_call_id)
        return accepted

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
            self._schedule_cancel_callback(callback)
            return
        self._cancel_callbacks.append(callback)

    def request_cancel(self, reason: str | None = None) -> None:
        """Request best-effort cancellation of this run."""
        if self.status != RunStatus.RUNNING or self.cancel_requested:
            return
        self.cancel_reason = reason
        self.cancel_requested = True
        # A Run cancel subsumes every still-active per-call cancel. Fire those
        # callbacks before cancelling the executor task so Tool-owned processes,
        # connections, and other resources receive their cleanup signal even
        # when they are not managed by the Run-level ProcessManager scope.
        # Registration order is stable and completed calls have already cleared
        # their entries, so only active calls participate.
        for tool_call_id in list(self._tool_cancel_callbacks):
            self.cancel_tool_call(tool_call_id)
        for callback in list(self._cancel_callbacks):
            self._schedule_cancel_callback(callback)
        if self._task is not None and self._execution_started:
            self._task.cancel()

    def register_tool_cancel(self, tool_call_id: str, callback: CancelCallback) -> None:
        """Register a per-tool-call cancel callback without cancelling the run."""
        if (
            self.cancel_requested
            or self._tool_cancel_callbacks.get(tool_call_id) is _CANCELLED_TOOL_CALL
        ):
            # Close both registration races: a callback may arrive after the
            # whole Run was cancelled or after this started Tool Call received
            # its own early cancel request.
            self._tool_cancel_callbacks[tool_call_id] = _CANCELLED_TOOL_CALL
            self._schedule_cancel_callback(callback)
            return
        self._tool_cancel_callbacks[tool_call_id] = callback

    def begin_tool_call(self, tool_call_id: str) -> None:
        """Make a started tool call cancellable before its callback is ready."""
        if self.cancel_requested:
            self._tool_cancel_callbacks[tool_call_id] = _CANCELLED_TOOL_CALL
            return
        self._tool_cancel_callbacks.setdefault(tool_call_id, _ACTIVE_TOOL_CALL)

    def cancel_tool_call(self, tool_call_id: str) -> bool:
        """Cancel a specific tool call without cancelling the run itself."""
        entry = self._tool_cancel_callbacks.get(tool_call_id)
        if entry is None or entry is _CANCELLED_TOOL_CALL:
            return False
        self._tool_cancel_callbacks[tool_call_id] = _CANCELLED_TOOL_CALL
        if self._tool_background_callbacks.pop(tool_call_id, None) is not None:
            self.emit("run_controls_changed", self.controls())
        if entry is not _ACTIVE_TOOL_CALL:
            self._schedule_cancel_callback(cast(CancelCallback, entry))
        return True

    def _schedule_cancel_callback(self, callback: CancelCallback) -> None:
        future = _schedule_callback(callback)
        if future is None:
            return
        if self._cancel_cleanup_expired:
            future.cancel()
            return
        self._cancel_cleanup_futures.add(future)
        future.add_done_callback(self._cancel_cleanup_futures.discard)

    async def _wait_for_cancel_cleanup(self) -> None:
        """Drain async cancellation callbacks within one shared time budget.

        Callbacks may register further cancellation work while an earlier
        callback is completing, so drain snapshots until the owned set is
        empty. Callback failures are logged by their completion callback and do
        not prevent the Run from reaching its terminal cancelled state. At the
        deadline, cancel remaining work without waiting for cancellation-resistant
        callbacks; cleanup remains best effort and the Session can advance.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CANCEL_CLEANUP_TIMEOUT_SECONDS
        while self._cancel_cleanup_futures:
            cleanup_futures = tuple(self._cancel_cleanup_futures)
            done, pending = await asyncio.wait(
                cleanup_futures,
                timeout=max(0.0, deadline - loop.time()),
            )
            # Retire settled Futures directly while preserving cleanup work
            # registered by a callback in this snapshot.
            self._cancel_cleanup_futures.difference_update(done)
            if pending or (self._cancel_cleanup_futures and loop.time() >= deadline):
                self._cancel_cleanup_expired = True
                _LOGGER.warning(
                    "Run cancel cleanup timed out (run=%s pending=%d)",
                    self.id,
                    len(self._cancel_cleanup_futures),
                )
                for future in self._cancel_cleanup_futures:
                    future.cancel()
                self._cancel_cleanup_futures.clear()
                return

    def tool_call_cancelled(self, tool_call_id: str) -> bool:
        """Return whether a tool call was user-cancelled."""
        return self._tool_cancel_callbacks.get(tool_call_id) is _CANCELLED_TOOL_CALL

    def clear_tool_cancel(self, tool_call_id: str) -> None:
        """Remove the per-tool-call cancel registry entry."""
        self._tool_cancel_callbacks.pop(tool_call_id, None)
        if self._tool_background_callbacks.pop(tool_call_id, None) is not None:
            self.emit("run_controls_changed", self.controls())

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
        if self._user_compaction_requested and event_type in {
            COMPACTION_STARTED_EVENT,
            COMPACTION_ABORTED_EVENT,
            COMPACTION_COMPLETED_EVENT,
        }:
            payload = {**(payload or {}), "requested_by_user": True}
        event = RunEvent(
            sequence=self._next_sequence,
            run_id=self.id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            project_id=self.project_id,
            run_kind=self.run_kind,
            contributes_to_agent_activity=self.contributes_to_agent_activity,
            type=event_type,
            payload=dict(payload or {}),
        )
        self._next_sequence += 1
        self._event_stream.publish(event)
        self.updated_at = event.timestamp
        if self.compaction_state != "unavailable":
            if event_type == COMPACTION_STARTED_EVENT:
                self.set_compaction_state("running")
            elif event_type in {COMPACTION_COMPLETED_EVENT, COMPACTION_ABORTED_EVENT}:
                self._user_compaction_requested = False
                self.set_compaction_state("idle")
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
        if self.status == RunStatus.INTERRUPTED and self.error is not None:
            raise self.error
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

    def mark_interrupted(
        self,
        error: RunInterruptedError,
        payload_extras: JsonObject | None = None,
    ) -> None:
        """Move the run to interrupted and publish the terminal event."""
        if self.status != RunStatus.RUNNING:
            return
        self.result = error.result
        self.error = error
        self.status = RunStatus.INTERRUPTED
        _LOGGER.warning(
            "Run %s interrupted after recovery was exhausted (agent=%s session=%s cause=%s)",
            self.id,
            self.agent_id,
            self.session_id,
            error.cause,
        )
        payload: JsonObject = {"status": self.status.value, "cause": error.cause}
        if payload_extras:
            payload.update(payload_extras)
        self.emit(RUN_INTERRUPTED_EVENT, payload)
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


def _schedule_callback(callback: CancelCallback) -> asyncio.Future[Any] | None:
    try:
        result = callback()
    except Exception:
        _LOGGER.warning("Run cancel callback failed", exc_info=True)
        return None
    if inspect.isawaitable(result):
        future = asyncio.ensure_future(cast(Coroutine[Any, Any, Any], result))
        future.add_done_callback(_on_cancel_callback_done)
        return future
    return None


def _on_cancel_callback_done(future: asyncio.Future[Any]) -> None:
    if future.cancelled():
        return
    try:
        future.result()
    except Exception:
        _LOGGER.warning("Run async cancel callback failed", exc_info=True)
