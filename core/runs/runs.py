"""Admission, Queue coordination and lifecycle ownership for Session Runs."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from core.runs.run import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_ABORTED_EVENT,
    COMPACTION_COMPLETED_EVENT,
    COMPACTION_STARTED_EVENT,
    DEFAULT_COMPLETED_RUN_RETENTION_LIMIT,
    DEFAULT_RUN_ADMISSION,
    DEFAULT_RUN_EVENT_RETENTION_LIMIT,
    DEFAULT_RUN_SUBSCRIBER_QUEUE_LIMIT,
    DEFAULT_WAITING_WORK_LIMIT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    MODEL_STEP_USAGE_EVENT,
    PROVIDER_HEARTBEAT_EVENT,
    PROVIDER_REQUEST_STATUS_EVENT,
    REASONING_DELTA_EVENT,
    REASONING_EVENT,
    RUN_AGENT_ACTIVITY_FIELD,
    RUN_CANCELLED_EVENT,
    RUN_CHANGE_STATS_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_INTERRUPTED_EVENT,
    RUN_KIND_FIELD,
    RUN_STARTED_EVENT,
    STREAM_ATTEMPT_RESTARTED_EVENT,
    TERMINAL_EVENT_TYPES,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    TOOL_CALL_STDERR_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    USER_MESSAGE_EVENT,
    ActiveRunError,
    CancelCallback,
    JsonObject,
    QueuedRunItem,
    Run,
    RunAdmission,
    RunAdmissionBlockedError,
    RunCancelledError,
    RunError,
    RunEvent,
    RunExecutionOwner,
    RunExecutor,
    RunInterruptedError,
    RunKind,
    RunNotFoundError,
    RunStatus,
    WaitingWorkAdmission,
    WaitingWorkLimitError,
)
from core.utils.ids import new_id
from core.utils.timestamps import format_canonical_timestamp

if TYPE_CHECKING:
    from core.sessions import SessionAddress

__all__ = [
    "ASSISTANT_OUTPUT_DELTA_EVENT",
    "ASSISTANT_OUTPUT_EVENT",
    "COMPACTION_ABORTED_EVENT",
    "COMPACTION_COMPLETED_EVENT",
    "COMPACTION_STARTED_EVENT",
    "DEFAULT_COMPLETED_RUN_RETENTION_LIMIT",
    "DEFAULT_RUN_ADMISSION",
    "DEFAULT_RUN_EVENT_RETENTION_LIMIT",
    "DEFAULT_RUN_SUBSCRIBER_QUEUE_LIMIT",
    "DEFAULT_WAITING_WORK_LIMIT",
    "ERROR_MESSAGE_PERSISTED_EVENT",
    "MODEL_FALLBACK_ACTIVATED_EVENT",
    "MODEL_STEP_USAGE_EVENT",
    "PROVIDER_HEARTBEAT_EVENT",
    "PROVIDER_REQUEST_STATUS_EVENT",
    "REASONING_DELTA_EVENT",
    "REASONING_EVENT",
    "RUN_AGENT_ACTIVITY_FIELD",
    "RUN_CANCELLED_EVENT",
    "RUN_CHANGE_STATS_EVENT",
    "RUN_COMPLETED_EVENT",
    "RUN_FAILED_EVENT",
    "RUN_INTERRUPTED_EVENT",
    "RUN_KIND_FIELD",
    "RUN_STARTED_EVENT",
    "STREAM_ATTEMPT_RESTARTED_EVENT",
    "TERMINAL_EVENT_TYPES",
    "TOOL_CALL_DELTA_EVENT",
    "TOOL_CALL_RESULT_EVENT",
    "TOOL_CALL_STARTED_EVENT",
    "TOOL_CALL_STDERR_EVENT",
    "TOOL_CALL_STDOUT_EVENT",
    "USER_MESSAGE_EVENT",
    "ActiveRunError",
    "CancelCallback",
    "JsonObject",
    "QueuedRunItem",
    "Run",
    "RunAdmission",
    "RunAdmissionBlockedError",
    "RunCancelledError",
    "RunError",
    "RunEvent",
    "RunExecutionOwner",
    "RunExecutor",
    "RunInterruptedError",
    "RunKind",
    "RunNotFoundError",
    "RunStatus",
    "WaitingWorkAdmission",
    "WaitingWorkLimitError",
    "ChatRunManager",
]

_LOGGER = logging.getLogger("vbot.runs")


def _session_address(project_id: str | None, agent_id: str, session_id: str) -> SessionAddress:
    """Build the ``SessionAddress`` used as this manager's internal session key.

    Imported lazily because ``core.sessions`` imports this package (``RunKind``);
    a module-level import would close an import cycle.
    """
    from core.sessions import SessionAddress

    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


class RunPersistence(Protocol):
    async def start_run(self, run: Run) -> None: ...

    async def finish_run(self, run: Run, status: str, payload: JsonObject) -> JsonObject: ...


class ChatRunManager:
    """Coordinate Runs with optional persistence supplied at construction.

    Runtime supplies its Session manager. Omitting persistence creates an
    in-memory manager; constructing Chat consumers never changes that choice.
    """

    def __init__(
        self,
        *,
        persistence: RunPersistence | None = None,
        completed_run_retention_limit: int = DEFAULT_COMPLETED_RUN_RETENTION_LIMIT,
        run_event_retention_limit: int = DEFAULT_RUN_EVENT_RETENTION_LIMIT,
        waiting_work_limit: int = DEFAULT_WAITING_WORK_LIMIT,
        admission_validator: Callable[[SessionAddress, RunAdmission], None] | None = None,
    ) -> None:
        if completed_run_retention_limit < 1:
            raise ValueError("completed_run_retention_limit must be positive")
        if run_event_retention_limit < 1:
            raise ValueError("run_event_retention_limit must be positive")
        if waiting_work_limit < 1:
            raise ValueError("waiting_work_limit must be positive")
        self._lock = asyncio.Lock()
        self._active_by_session: dict[SessionAddress, Run] = {}
        self._queues: dict[SessionAddress, deque[QueuedRunItem]] = {}
        self._guarded_sessions: set[SessionAddress] = set()
        self._guarded_agents: set[tuple[str | None, str]] = set()
        self._guarded_projects: set[str] = set()
        self._waiting_work_admissions: dict[str, WaitingWorkAdmission] = {}
        self._runs: dict[str, Run] = {}
        self._run_started_callbacks: list[Callable[[Run], None]] = []
        self._completed_run_retention_limit = completed_run_retention_limit
        self._run_event_retention_limit = run_event_retention_limit
        self._waiting_work_limit = waiting_work_limit
        self._closed = False
        self._persistence = persistence
        self._admission_validator = admission_validator
        self._maintenance_operation_id: str | None = None
        self._maintenance_origin: tuple[SessionAddress, str] | None = None

    async def maintenance_begin(
        self,
        operation_id: str,
        *,
        origin: tuple[SessionAddress, str] | None = None,
    ) -> dict[str, object]:
        """Reject new admission while already accepted work drains."""
        if not operation_id:
            raise ValueError("maintenance operation id must not be empty")
        async with self._lock:
            if self._maintenance_operation_id not in {None, operation_id}:
                raise RunAdmissionBlockedError("another maintenance operation is active")
            self._maintenance_operation_id = operation_id
            if origin is not None:
                if self._maintenance_origin not in {None, origin}:
                    raise RunAdmissionBlockedError("maintenance origin does not match")
                self._maintenance_origin = origin
            return self._maintenance_status_locked(operation_id)

    async def maintenance_status(self, operation_id: str) -> dict[str, object]:
        """Return drain state for the exact active maintenance operation."""
        async with self._lock:
            return self._maintenance_status_locked(operation_id)

    async def maintenance_end(self, operation_id: str) -> dict[str, object]:
        """Release admission for the exact operation; repeated release is safe."""
        async with self._lock:
            if self._maintenance_operation_id is None:
                return {"operation_id": operation_id, "active": False}
            if self._maintenance_operation_id != operation_id:
                raise RunAdmissionBlockedError("another maintenance operation is active")
            self._maintenance_operation_id = None
            self._maintenance_origin = None
            return {"operation_id": operation_id, "active": False}

    def _maintenance_status_locked(self, operation_id: str) -> dict[str, object]:
        if self._maintenance_operation_id != operation_id:
            raise RunAdmissionBlockedError("maintenance operation is not active")
        active_runs = [
            (address, run)
            for address, run in self._active_by_session.items()
            if run.status == RunStatus.RUNNING
        ]
        queued_count = sum(len(queue) for queue in self._queues.values())
        reservation_count = len(self._waiting_work_admissions)
        origin_pending = False
        blocking_active = len(active_runs)
        if self._maintenance_origin is not None:
            origin_address, origin_run_id = self._maintenance_origin
            origin_pending = any(
                address == origin_address and run.id == origin_run_id
                for address, run in active_runs
            )
        return {
            "operation_id": operation_id,
            "active": True,
            "active_count": len(active_runs),
            "queued_count": queued_count,
            "reservation_count": reservation_count,
            "origin_pending": origin_pending,
            "safe_to_stop": blocking_active == 0 and queued_count == 0 and reservation_count == 0,
        }

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
        if self._closed:
            raise RunAdmissionBlockedError("run manager is shutting down")
        if self._maintenance_operation_id is not None:
            raise RunAdmissionBlockedError("run manager is draining for maintenance")

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
    async def session_admission_guard(
        self,
        *session_addresses: SessionAddress,
    ) -> AsyncIterator[None]:
        """Hold one atomic no-Run boundary across a Session transition.

        Every supplied Session must be idle when the guard is acquired. While
        held, both immediate starts and queued admission are rejected. Supplying
        source and destination addresses together protects an Agent Takeover
        through the destination divider/note writes as one transition.
        """
        guarded_sessions = frozenset(session_addresses)
        if not guarded_sessions:
            raise ValueError("session admission guard requires at least one session")

        async with self._lock:
            if any(
                self._has_activity_for_session_locked(address)
                or self._run_admission_is_guarded_locked(address, working_project_id=None)
                for address in guarded_sessions
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
                    guarded.project_id == project_id and guarded.agent_id == agent_id
                    for guarded in self._guarded_sessions
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
                or any(guarded.project_id == project_id for guarded in self._guarded_sessions)
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
        address: SessionAddress,
        executor: RunExecutor,
        *,
        admission: RunAdmission = DEFAULT_RUN_ADMISSION,
    ) -> Run:
        """Start one run if the session has no active run.

        ``address`` is the required session identity (see ``SessionAddress``);
        the created ``Run`` reads its project anchor, agent, and session from
        it. ``admission`` carries the immutable origin/activity decisions.
        """
        async with self._lock:
            if self._closed:
                raise RunAdmissionBlockedError("run manager is shutting down")
            if self._maintenance_operation_id is not None:
                raise RunAdmissionBlockedError("run manager is draining for maintenance")
            self._ensure_run_admission_allowed_locked(address, admission)
            active_run = self._active_by_session.get(address)
            if active_run is not None and active_run.status == RunStatus.RUNNING:
                raise ActiveRunError(f"session already has an active run: {address.session_id}")
            return self._start_run_locked(
                address=address,
                executor=executor,
                admission=admission,
            )

    async def enqueue(
        self,
        address: SessionAddress,
        executor: RunExecutor,
        *,
        display_content: str = "",
        editable: bool = False,
        steerable: bool = False,
        internal: bool = False,
        waiting_work_admission: WaitingWorkAdmission | None = None,
        admission: RunAdmission = DEFAULT_RUN_ADMISSION,
    ) -> QueuedRunItem:
        """Start immediately when idle or append one item to the session queue."""
        future: asyncio.Future[Run] = asyncio.get_running_loop().create_future()
        item = QueuedRunItem(
            item_id=new_id("que"),
            display_content=display_content,
            executor=executor,
            internal=internal,
            future=future,
            editable=editable,
            steerable=steerable,
            admission=admission,
        )

        def remove_abandoned_item(completed_future: asyncio.Future[Run]) -> None:
            # Awaiting a bare Future propagates task cancellation into that Future.
            # The Future has one owner, so cancellation means the accepted work was
            # abandoned and must not remain queued to execute without a consumer.
            if completed_future.cancelled():
                self.remove_queued(
                    address.agent_id,
                    address.session_id,
                    item.item_id,
                    project_id=address.project_id,
                )

        item.future.add_done_callback(remove_abandoned_item)

        async with self._lock:
            if self._closed:
                item.future.cancel()
                raise RunAdmissionBlockedError("run manager is shutting down")
            if self._maintenance_operation_id is not None and (
                waiting_work_admission is None
                or self._waiting_work_admissions.get(waiting_work_admission.id)
                != waiting_work_admission
            ):
                item.future.cancel()
                raise RunAdmissionBlockedError("run manager is draining for maintenance")
            try:
                self._ensure_run_admission_allowed_locked(address, admission)
            except RunAdmissionBlockedError:
                item.future.cancel()
                raise
            active_run = self._active_by_session.get(address)
            if active_run is None or active_run.status != RunStatus.RUNNING:
                self._consume_waiting_work_admission(waiting_work_admission)
                run = self._start_run_locked(
                    address=address,
                    executor=item.executor,
                    queue_item_id=item.item_id,
                    admission=item.admission,
                )
                item.future.set_result(run)
                return item

            waiting_scope = self._consume_waiting_work_admission(waiting_work_admission)
            if waiting_scope is None and self._waiting_work_count() >= self._waiting_work_limit:
                _LOGGER.warning(
                    "Run rejected by global waiting-work limit (agent=%s session=%s limit=%d)",
                    address.agent_id,
                    address.session_id,
                    self._waiting_work_limit,
                )
                item.future.cancel()
                raise WaitingWorkLimitError("global waiting work limit reached")

            item.waiting_scope = waiting_scope
            queue = self._queues.setdefault(address, deque())
            queue.append(item)
            _LOGGER.info(
                "Run queued for busy session (agent=%s session=%s queue_depth=%d)",
                address.agent_id,
                address.session_id,
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
        address = _session_address(project_id, agent_id, session_id)
        return list(self._queues.get(address, ()))

    def all_queued(self) -> list[tuple[SessionAddress, QueuedRunItem]]:
        """Return a fresh snapshot of queued items across every session."""
        return [(address, item) for address, queue in self._queues.items() for item in queue]

    def remove_queued(
        self, agent_id: str, session_id: str, item_id: str, *, project_id: str | None
    ) -> bool:
        """Remove one queued item if present."""
        address = _session_address(project_id, agent_id, session_id)
        queue = self._queues.get(address)
        if queue is None:
            return False

        for item in queue:
            if item.item_id != item_id:
                continue
            if item.steering_in_flight:
                return False
            queue.remove(item)
            if not item.future.done():
                item.future.cancel()
            if not queue:
                self._queues.pop(address, None)
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
        editable: bool | None = None,
    ) -> bool:
        """Replace the queued executor and display text for one item."""
        address = _session_address(project_id, agent_id, session_id)
        queue = self._queues.get(address)
        if queue is None:
            return False

        for item in queue:
            if item.item_id != item_id:
                continue
            if item.steering_run_id is not None:
                return False
            item.executor = new_executor
            item.display_content = new_display_content
            if editable is not None:
                item.editable = editable
            return True
        return False

    def steer_queued(
        self,
        agent_id: str,
        session_id: str,
        item_id: str,
        *,
        project_id: str | None,
        run_id: str,
    ) -> QueuedRunItem:
        """Bind a queued input to the exact active Run without removing it yet."""
        run = self.active_run(agent_id=agent_id, session_id=session_id, project_id=project_id)
        if run is None or run.id != run_id or not run.accepts_steering or run.cancel_requested:
            raise ActiveRunError("the selected Run no longer accepts steering")
        for item in self.list_queued(agent_id, session_id, project_id=project_id):
            if item.item_id != item_id:
                continue
            if (
                not item.steerable
                or item.internal
                or item.admission.owner is not None
                or run.execution_owner is not None
            ):
                raise ActiveRunError("this queued input cannot steer a Run")
            if item.admission.working_project_id != run.working_project_id:
                raise ActiveRunError("queued input has a different working Project")
            if item.steering_run_id != run.id:
                item.steering_run_id = run.id
                _LOGGER.info("Queue steering requested (run=%s item=%s)", run.id, item.item_id)
            return item
        raise RunNotFoundError(f"queued item not found: {item_id}")

    def pending_steering(self, run: Run) -> list[QueuedRunItem]:
        """Snapshot requested input in Queue order at a safe executor boundary."""
        return [
            item
            for item in self.list_queued(run.agent_id, run.session_id, project_id=run.project_id)
            if item.steering_run_id == run.id and not item.future.cancelled()
        ]

    async def deliver_steering(
        self, run: Run, persist: Callable[[QueuedRunItem], Awaitable[None]]
    ) -> bool:
        """Keep input queued until its append succeeds, without locking across I/O."""
        delivered = False
        for item in self.pending_steering(run):
            # Another selected item can be removed while the previous append awaits I/O.
            if item.future.done():
                continue
            run.raise_if_cancelled()
            item.steering_in_flight = True
            try:
                await persist(item)
                address = _session_address(run.project_id, run.agent_id, run.session_id)
                queue = self._queues.get(address)
                if queue is not None and item in queue:
                    queue.remove(item)
                    if not queue:
                        self._queues.pop(address, None)
                if not item.future.done():
                    item.future.set_result(run)
                delivered = True
            finally:
                item.steering_in_flight = False
        return delivered

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
        address = _session_address(project_id, agent_id, session_id)
        run = self._active_by_session.get(address)
        if run is None or run.status != RunStatus.RUNNING:
            raise RunNotFoundError(f"no active run for agent '{agent_id}' session '{session_id}'")
        run.request_cancel(reason=reason)
        return run

    def active_run(self, *, agent_id: str, session_id: str, project_id: str | None) -> Run | None:
        """Return the active run for a session, if one exists."""
        address = _session_address(project_id, agent_id, session_id)
        run = self._active_by_session.get(address)
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

    async def aclose(self) -> None:
        """Reject new work, cancel queued items, and drain every active Run."""
        async with self._lock:
            if self._closed:
                active_runs = list(self._active_by_session.values())
            else:
                self._closed = True
                for queue in self._queues.values():
                    for item in queue:
                        if not item.future.done():
                            item.future.cancel()
                self._queues.clear()
                self._waiting_work_admissions.clear()
                active_runs = list(self._active_by_session.values())
        for run in active_runs:
            run.request_cancel(reason="shutdown")
        active_tasks = [
            run._task  # noqa: SLF001 - manager owns Run execution tasks.
            for run in active_runs
            if run._task is not None  # noqa: SLF001
        ]
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

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
        on the exact ``SessionAddress`` both the active-run and the queue maps
        use. Destructive lifecycle workflows use
        :meth:`session_admission_guard` instead because this snapshot alone
        cannot prevent a new Run from entering after the check.
        """
        address = _session_address(project_id, agent_id, session_id)
        return self._has_activity_for_session_locked(address)

    def _ensure_run_admission_allowed_locked(
        self, address: SessionAddress, admission: RunAdmission
    ) -> None:
        if self._run_admission_is_guarded_locked(address, admission.working_project_id):
            raise RunAdmissionBlockedError(
                "run admission is blocked while its Session, Agent, or Project is transitioning"
            )
        if self._admission_validator is not None:
            self._admission_validator(address, admission)

    def _run_admission_is_guarded_locked(
        self, address: SessionAddress, working_project_id: str | None
    ) -> bool:
        return (
            address in self._guarded_sessions
            or (address.project_id, address.agent_id) in self._guarded_agents
            or (address.project_id is not None and address.project_id in self._guarded_projects)
            or (working_project_id is not None and working_project_id in self._guarded_projects)
        )

    def _has_activity_for_session_locked(self, address: SessionAddress) -> bool:
        active_run = self._active_by_session.get(address)
        if active_run is not None and active_run.status == RunStatus.RUNNING:
            return True
        return bool(self._queues.get(address))

    def _has_activity_for_agent_locked(self, agent_key: tuple[str | None, str]) -> bool:
        project_id, agent_id = agent_key
        if any(
            active_address.project_id == project_id
            and active_address.agent_id == agent_id
            and run.status == RunStatus.RUNNING
            for active_address, run in self._active_by_session.items()
        ):
            return True
        return any(
            queued_address.project_id == project_id
            and queued_address.agent_id == agent_id
            and bool(queue)
            for queued_address, queue in self._queues.items()
        )

    def _has_activity_for_working_project_locked(self, project_id: str) -> bool:
        if any(
            run.status == RunStatus.RUNNING and run.working_project_id == project_id
            for run in self._active_by_session.values()
        ):
            return True
        return any(
            item.admission.working_project_id == project_id
            for queue in self._queues.values()
            for item in queue
        )

    def _has_activity_for_project_locked(self, project_id: str) -> bool:
        if self._has_activity_for_working_project_locked(project_id):
            return True
        if any(
            active_address.project_id == project_id and run.status == RunStatus.RUNNING
            for active_address, run in self._active_by_session.items()
        ):
            return True
        return any(
            queued_address.project_id == project_id and bool(queue)
            for queued_address, queue in self._queues.items()
        )

    async def _release_session_admission_guard(
        self, guarded_sessions: frozenset[SessionAddress]
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
        address: SessionAddress,
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
                "started_at": format_canonical_timestamp(timing_started_at),
                "completed_at": format_canonical_timestamp(completed_at),
                "duration_ms": duration_ms,
            }

        def terminal_extras() -> JsonObject:
            extras: JsonObject = dict(run.terminal_payload_extras)
            extras["iteration_count"] = run.iteration_count
            extras["timing"] = terminal_timing()
            return extras

        result = None
        error: BaseException | None = None
        status = RunStatus.COMPLETED
        abort: BaseException | None = None
        admitted = False
        try:
            if self._persistence is not None:
                admission = asyncio.create_task(self._persistence.start_run(run))
                try:
                    await asyncio.shield(admission)
                except asyncio.CancelledError:
                    # Admission can already be committing in the writer thread.
                    # Observe its outcome before attempting terminal persistence.
                    await admission
                    admitted = True
                    run._mark_admitted()  # noqa: SLF001 - manager owns run lifecycle internals.
                    raise
                admitted = True
            run._mark_admitted()  # noqa: SLF001 - manager owns run lifecycle internals.
            run.raise_if_cancelled()
            started_payload: JsonObject = {"status": RunStatus.RUNNING.value}
            if run._started_from_queue_item_id is not None:  # noqa: SLF001
                started_payload["queue_item_id"] = run._started_from_queue_item_id  # noqa: SLF001
            run.emit(RUN_STARTED_EVENT, started_payload)
            result = await executor(run)
        except RunInterruptedError as exc:
            status, error = RunStatus.INTERRUPTED, exc
        except asyncio.CancelledError as exc:
            status, error = RunStatus.CANCELLED, exc
        except Exception as exc:
            status, error = RunStatus.FAILED, exc
        except BaseException as exc:
            status, abort = RunStatus.CANCELLED, exc
        finally:
            try:
                if run.cancel_requested or status == RunStatus.CANCELLED:
                    status = RunStatus.CANCELLED
                    await run._wait_for_cancel_cleanup()  # noqa: SLF001
            except BaseException as exc:
                status, abort = RunStatus.CANCELLED, exc
            run._completion_started = True  # noqa: SLF001 - freeze outcome before the commit.
            payload = terminal_extras()
            usage = getattr(result, "usage", None)
            if usage:
                payload["usage"] = usage
            try:
                if self._persistence is not None and admitted:
                    payload.update(await self._persistence.finish_run(run, status.value, payload))
                elif self._persistence is not None:
                    payload["history_persisted"] = False
            except Exception as exc:
                # No terminal acknowledgement may claim a durable completion
                # when its transaction failed. The running row is recoverable.
                _LOGGER.error("Run completion persistence failed: %s", run.id, exc_info=True)
                status, error = RunStatus.FAILED, exc
                payload["history_persisted"] = False
            except BaseException as exc:
                status, abort = RunStatus.CANCELLED, exc
                payload["history_persisted"] = False
            try:
                notification_errors = await run.notify_completion(status)
            except BaseException as exc:
                # Even process-level observer aborts must release the Session.
                # Preserve the already committed outcome before propagating.
                abort = exc
                notification_errors = [str(exc) or type(exc).__name__]
            if notification_errors:
                payload["completion_notification_errors"] = notification_errors
            if status == RunStatus.CANCELLED:
                run.mark_cancelled(payload_extras=payload)
            elif status == RunStatus.INTERRUPTED:
                assert isinstance(error, RunInterruptedError)
                run.mark_interrupted(error, payload_extras=payload)
            elif status == RunStatus.FAILED:
                assert isinstance(error, Exception)
                run.mark_failed(error, payload_extras=payload)
            else:
                run.mark_completed(result, payload_extras=payload)
            async with self._lock:
                if self._active_by_session.get(address) is run:
                    self._active_by_session.pop(address, None)
                self._prune_terminal_runs_locked()
            await self._drain_next(address)
        if abort is not None:
            raise abort

    async def _drain_next(self, address: SessionAddress) -> None:
        async with self._lock:
            if self._closed:
                closed_queue = self._queues.pop(address, ())
                for item in closed_queue:
                    if not item.future.done():
                        item.future.cancel()
                return
            active_run = self._active_by_session.get(address)
            if active_run is not None and active_run.status == RunStatus.RUNNING:
                return

            queue = self._queues.get(address)
            if not queue:
                self._queues.pop(address, None)
                return

            for pending in queue:
                pending.steering_run_id = None
            while queue:
                item = queue.popleft()
                # The cancellation callback normally removes an abandoned item
                # immediately. This guard closes the same-tick race where the
                # active Run drains before that callback gets its event-loop turn.
                if item.future.done():
                    continue
                if not queue:
                    self._queues.pop(address, None)

                try:
                    self._ensure_run_admission_allowed_locked(address, item.admission)
                    run = self._start_run_locked(
                        address=address,
                        executor=item.executor,
                        queue_item_id=item.item_id,
                        admission=item.admission,
                    )
                except RunAdmissionBlockedError as error:
                    item.future.set_exception(error)
                    continue
                except Exception as error:
                    # One broken item must not strand the rest of the Queue. Its
                    # owner receives the failure; the remaining items keep draining.
                    _LOGGER.error(
                        "Queued Run could not start (agent=%s session=%s item=%s)",
                        address.agent_id,
                        address.session_id,
                        item.item_id,
                        exc_info=True,
                    )
                    item.future.set_exception(error)
                    continue
                item.future.set_result(run)
                return

            self._queues.pop(address, None)

    def _start_run_locked(
        self,
        *,
        address: SessionAddress,
        executor: RunExecutor,
        queue_item_id: str | None = None,
        admission: RunAdmission = DEFAULT_RUN_ADMISSION,
    ) -> Run:
        # The address is the single source of the run's identity: the project
        # anchor, agent, and session all come from it, so a drained queue item
        # can never start under a different anchor than it was enqueued for.
        run = Run(
            run_id=new_id("run"),
            agent_id=address.agent_id,
            session_id=address.session_id,
            project_id=address.project_id,
            working_project_id=admission.working_project_id,
            run_kind=admission.run_kind,
            contributes_to_agent_activity=admission.contributes_to_agent_activity,
            work_id=admission.work_id,
            execution_owner=admission.owner,
            execution_input_id=admission.input_id,
            source_session_id=admission.source_session_id,
            event_retention_limit=self._run_event_retention_limit,
        )
        run._started_from_queue_item_id = queue_item_id  # noqa: SLF001 - run carries its own start origin.
        # Create the task before registering the Run: a failure here leaves no
        # active Session entry behind. The task cannot run before this returns.
        task = asyncio.create_task(self._execute(run, address, executor))
        self._active_by_session[address] = run
        self._runs[run.id] = run
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
