"""Run admission, terminal persistence and restart recovery at the Session boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from core.runs import RunStatus
from core.sessions._types import (
    JsonObject,
    SessionAddress,
    SessionRunAdmission,
    SessionRunCompletion,
)
from core.sessions.store import SessionStore
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runs import Run


_LOGGER = get_logger("sessions")


class SessionRunPersistence:
    """Translate live Runs into canonical Session execution records and notifications."""

    def __init__(
        self, store: SessionStore, completion_read: Callable[[SessionAddress, str], None]
    ) -> None:
        self._store = store
        self._completion_read = completion_read

    def recover_interrupted_runs(self) -> None:
        """Settle the Runs a previous process left running, each with its terminal line.

        These Runs never reach the Run manager, which logs every other Run's outcome.
        """
        for run in self._store.execution.recover_interrupted_runs():
            address = run.address
            project = "" if address.project_id is None else f" project={address.project_id}"
            _LOGGER.warning(
                "Run interrupted (run=%s agent=%s session=%s%s kind=%s started=%s "
                "iterations=%d cause=process_restart)",
                run.run_id,
                address.agent_id,
                address.session_id,
                project,
                run.run_kind,
                run.started_at,
                run.iteration_count,
            )

    async def start_run(self, run: Run) -> None:
        """Admit *run* with its Run kind and execution owner in one transaction.

        A Session the admission creates works in the Run's working Project.
        """
        address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        admission = SessionRunAdmission(
            run_id=run.id,
            run_kind=run.run_kind.value,
            started_at=run.created_at,
            work_id=run.work_id,
            contributes_to_activity=run.contributes_to_agent_activity,
            owner=run.execution_owner,
            input_id=run.execution_input_id,
            expected_generation_id=run.expected_session_generation_id,
            working_project_id=None if run.project_id is not None else run.working_project_id,
            accessor_input_id=run.accessor_input_id,
            schedule_id=run.schedule_id,
            notification_policy=run.notification_policy() if run.notification_policy else "all",
            notification_eligible=bool(
                run.completion_activity and run.completion_activity(RunStatus.INTERRUPTED, None)
            ),
        )
        await self._store.run_async(self._store.execution.admit_run, address, admission)

    async def finish_run(
        self, run: Run, status: str, payload: JsonObject, *, completion_reason: str | None
    ) -> JsonObject:
        address = SessionAddress(
            project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
        )
        completion = SessionRunCompletion(
            run_id=run.id,
            work_id=run.work_id,
            status=status,
            timing=payload["timing"],
            iteration_count=run.iteration_count,
            change_stats=payload.get("change_stats"),
            completion_reason=completion_reason,
            notification_policy=run.notification_policy() if run.notification_policy else None,
            contributes_to_activity=run.contributes_to_agent_activity,
        )
        return await self._store.run_async(self._store.execution.finish_run, address, completion)

    def mark_terminal_run_read(self, address: SessionAddress, run_id: str) -> JsonObject:
        activity, marked = self._store.execution.mark_terminal_run_read(address, run_id)
        result = dict(activity)
        result["marked_read"] = marked
        if marked:
            self._completion_read(address, run_id)
        return result
