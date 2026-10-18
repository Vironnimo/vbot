"""Cron Run admission, terminal outcomes and post-fire persistence."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.automation import _cron_events as _events
from core.automation import _cron_schedule as _schedule
from core.automation import _cron_timing as _timing
from core.automation._cron_jobs import (
    MAX_CONSECUTIVE_CRON_FAILURES,
    CronJob,
    CronStorageError,
    _target_validation_error,
    _truncate_error,
)
from core.projects import (
    AgentResolutionError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    format_agent_address,
)
from core.runs import RunCancelledError, RunKind
from core.sessions import SessionNotFoundError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.automation.cron import CronService


@dataclass(frozen=True)
class CronRunOutcome:
    """Whether a fire admitted a Run and whether that Run succeeded."""

    succeeded: bool
    admitted: bool


class CronExecution:
    def __init__(self, owner: CronService) -> None:
        self._owner = owner

    async def _trigger_job_run(
        self,
        job: CronJob,
        *,
        owed: _schedule.OwedFire | None = None,
        event_due: _events.EventDue | None = None,
    ) -> CronRunOutcome:
        admitted = False
        self._owner._executing_jobs.add(job.id)
        try:
            async with self._owner._run_slots:
                latest = self._owner._jobs.get(job.id)
                if (
                    latest is None
                    or latest.status != "active"
                    or job.id in self._owner._pending_restarts
                ):
                    return CronRunOutcome(succeeded=False, admitted=admitted)

                if event_due is not None:
                    prepared = await self._owner._scheduler._prepare_event_fire(latest)
                    if prepared is None:
                        return CronRunOutcome(succeeded=False, admitted=admitted)
                    latest, event_due = prepared
                    owed = _schedule.OwedFire(due_at=event_due.due_at)
                else:
                    latest.last_attempt_at = _timing._utc_now_iso()
                    latest.last_error = None
                    self._owner._jobs[latest.id] = latest
                    await self._save_jobs_after_fire(latest.id)
                # Before admission begins, a scheduling edit withdraws this fire;
                # the replacement task must wait for the new schedule.
                latest = self._owner._jobs.get(job.id)
                if (
                    latest is None
                    or latest.status != "active"
                    or job.id in self._owner._pending_restarts
                ):
                    return CronRunOutcome(succeeded=False, admitted=admitted)
                # Capacity and claim persistence can delay even an on-time fire.
                # Keep its due-time evidence until all pre-admission waits settle.
                late = self._owner._scheduler._late_fire(owed) if owed is not None else None
                _LOGGER.info(
                    "Cron job fired (job=%s agent=%s session=%s%s%s)",
                    latest.id,
                    latest.agent_id,
                    latest.session_id,
                    f" project={latest.project_id}" if latest.project_id else "",
                    (
                        f" late_by={late.late_by_seconds}s"
                        f" earlier_missed={late.owed.earlier_missed}"
                        if late is not None
                        else ""
                    ),
                )
                # Omitted when there is nothing to note, so the call shape stays unchanged.
                context = self._owner._scheduler._run_context(latest, late, event_due)
                note: dict[str, Any] = {"context_note": context} if context else {}
                run: Any | None = None
                try:
                    run = await self._owner._trigger_service.trigger_run(
                        latest.agent_id,
                        latest.prompt,
                        latest.session_id,
                        project_id=latest.project_id,
                        run_kind=RunKind.CRON,
                        contributes_to_agent_activity=False,
                        **note,
                    )
                    admitted = True
                    latest = self._owner._jobs.get(job.id)
                    if latest is None:
                        wait_for_run = getattr(run, "wait", None)
                        if callable(wait_for_run):
                            await wait_for_run()
                        return CronRunOutcome(succeeded=False, admitted=admitted)
                    latest.last_fired_at = _timing._utc_now_iso()
                    run_id = getattr(run, "id", None)
                    latest.last_run_id = run_id if isinstance(run_id, str) else None
                    if latest.remaining_runs is not None:
                        latest.remaining_runs = max(latest.remaining_runs - 1, 0)
                    self._owner._jobs[latest.id] = latest
                    await self._persist_after_fire(latest.id)

                    wait_for_run = getattr(run, "wait", None)
                    if callable(wait_for_run):
                        await wait_for_run()
                except asyncio.CancelledError:
                    raise
                except RunCancelledError as error:
                    if run is not None:
                        await self._record_run_failure(job.id, error)
                        await self._finalize_exhausted_job(job.id)
                    else:
                        await self._record_trigger_failure(job.id, error)
                        latest = self._owner._jobs.get(job.id)
                        if latest is not None and latest.schedule_type == "once":
                            latest.status = "failed"
                            await self._save_jobs_after_fire(latest.id)
                        if latest is not None:
                            # No Run started, so no Run line reports this firing.
                            _LOGGER.log(
                                logging.WARNING if latest.status == "failed" else logging.INFO,
                                "Cron job Run cancelled before admission (job=%s status=%s)",
                                job.id,
                                latest.status,
                            )
                    return CronRunOutcome(succeeded=False, admitted=admitted)
                except Exception as error:
                    if run is None and await self._record_unavailable_target(job.id, error):
                        return CronRunOutcome(succeeded=False, admitted=admitted)
                    if run is None:
                        # Pre-admission failure - the Run never started. A full Queue
                        # or a shutdown window must not burn a recurring job's
                        # five-strike execution-failure budget; once jobs keep their
                        # own fire-claim retry via the False return either way.
                        await self._record_trigger_failure(job.id, error)
                        _LOGGER.error(
                            "Cron job trigger failed before admission for job=%s: %s",
                            job.id,
                            error,
                            exc_info=(type(error), error, error.__traceback__),
                        )
                        return CronRunOutcome(succeeded=False, admitted=admitted)
                    await self._record_run_failure(job.id, error)
                    await self._finalize_exhausted_job(job.id)
                    _LOGGER.error(
                        "Cron job Run failed for job=%s: %s",
                        job.id,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                    return CronRunOutcome(succeeded=False, admitted=admitted)

                latest = self._owner._jobs.get(job.id)
                if latest is None:
                    return CronRunOutcome(succeeded=False, admitted=admitted)
                latest.last_completed_at = _timing._utc_now_iso()
                latest.last_outcome = "success"
                latest.last_error = None
                latest.consecutive_failures = 0
                self._owner._jobs[latest.id] = latest
                await self._save_jobs_after_fire(latest.id)
                await self._finalize_exhausted_job(latest.id)
                return CronRunOutcome(succeeded=True, admitted=admitted)
        finally:
            self._owner._executing_jobs.discard(job.id)

    async def _record_run_failure(self, job_id: str, error: BaseException) -> None:
        if self._note_run_failure(job_id, error):
            await self._save_jobs_after_fire(job_id)

    def _note_run_failure(self, job_id: str, error: BaseException) -> bool:
        """Account one failed execution in memory; ``False`` when the job is gone."""
        job = self._owner._jobs.get(job_id)
        if job is None:
            return False
        job.last_completed_at = _timing._utc_now_iso()
        job.last_outcome = "cancelled" if type(error).__name__ == "RunCancelledError" else "failed"
        job.last_error = _truncate_error(str(error) or type(error).__name__)
        _count_consecutive_failure(job)
        self._owner._jobs[job_id] = job
        return True

    async def _record_unavailable_target(self, job_id: str, error: Exception) -> bool:
        """Record a fire that could not start because its target is unavailable.

        Returns ``False`` for any other error, which the caller records as a
        trigger failure. No Run started, so no finite run is consumed. A pinned
        Session or a target Agent or Project that no longer exists lasts until
        the job is edited (for example, an Agent Takeover moved the Session, or
        the Agent was removed), so unlike a capacity or shutdown rejection it
        counts toward the stop rule like a failed Run; a once job keeps its
        bounded fire retries. A single miss, such as one during an Agent rename,
        is cleared by the next success. A target that exists but cannot run (for
        example, no usable Model) can recover without an edit, so it is recorded
        without counting.
        """
        if not isinstance(error, (SessionNotFoundError, AgentResolutionError)):
            return False
        job = self._owner._jobs.get(job_id)
        if job is None:
            return True
        if isinstance(error, SessionNotFoundError):
            reason = _missing_session_text(job)
            counts = True
        else:
            reason = str(_target_validation_error(job, error))
            counts = isinstance(
                error, (ResolutionAgentNotFoundError, ResolutionProjectNotFoundError)
            )
        job.last_outcome = "failed"
        job.last_error = _truncate_error(reason)
        _LOGGER.warning(
            "Cron job could not start its Run (job=%s reason=%s)", job_id, job.last_error
        )
        if counts:
            _count_consecutive_failure(job)
        self._owner._jobs[job_id] = job
        await self._save_jobs_after_fire(job_id)
        return True

    async def _record_trigger_failure(self, job_id: str, error: BaseException) -> None:
        """Record a pre-admission trigger failure without execution accounting.

        The Run never started, so ``consecutive_failures`` must not advance and a
        recurring job must not fatal-stop over capacity or shutdown windows. The
        error stays visible on the job for operators.
        """
        job = self._owner._jobs.get(job_id)
        if job is None:
            return
        job.last_outcome = "failed"
        job.last_error = _truncate_error(str(error) or type(error).__name__)
        self._owner._jobs[job_id] = job
        await self._save_jobs_after_fire(job_id)

    async def _finalize_exhausted_job(self, job_id: str) -> None:
        job = self._owner._jobs.get(job_id)
        if job is None or job.status != "active" or job.remaining_runs != 0:
            return
        job.status = "completed" if job.last_outcome == "success" else "failed"
        self._owner._jobs[job_id] = job
        await self._save_jobs_after_fire(job_id)
        _LOGGER.log(
            logging.INFO if job.status == "completed" else logging.WARNING,
            "Cron job finished its last run (job=%s outcome=%s status=%s)",
            job_id,
            job.last_outcome,
            job.status,
        )

    async def _persist_after_fire(self, job_id: str) -> bool:
        """Persist job state after a fire, giving up after bounded retries.

        A persistently unwritable jobs file must not hang the firing task
        forever: the previous unbounded retry loop stalled the job task for as
        long as the storage fault lasted. After the cap this logs at error and
        continues - in-memory state stays authoritative for the process, and
        the next successful save anywhere re-syncs the file.
        """
        for _attempt in range(_POST_FIRE_SAVE_MAX_ATTEMPTS):
            if await self._save_jobs_after_fire(job_id):
                return True
            await _timing._sleep(_POST_FIRE_SAVE_RETRY_SECONDS)
        _LOGGER.error(
            "Cron job state could not be persisted after %d attempts (job=%s); "
            "continuing with in-memory state",
            _POST_FIRE_SAVE_MAX_ATTEMPTS,
            job_id,
        )
        return False

    async def _save_jobs_after_fire(self, job_id: str) -> bool:
        try:
            await self._owner._save_jobs_async()
        except CronStorageError as error:
            _log_fire_save_failure(job_id, error)
            return False

        self._owner._notify_changed()
        return True


_POST_FIRE_SAVE_MAX_ATTEMPTS = 3


_POST_FIRE_SAVE_RETRY_SECONDS = 5.0


def _count_consecutive_failure(job: CronJob) -> None:
    """Count one failed fire; a recurring job stops as ``failed`` at the limit."""
    job.consecutive_failures += 1
    if (
        job.schedule_type != "once"
        and job.status != "failed"
        and job.consecutive_failures >= MAX_CONSECUTIVE_CRON_FAILURES
    ):
        job.status = "failed"
        _LOGGER.warning(
            "Cron job stopped after consecutive failures (job=%s failures=%d status=%s)",
            job.id,
            job.consecutive_failures,
            job.status,
        )


def _missing_session_text(job: CronJob) -> str:
    target = format_agent_address(job.agent_id, job.project_id)
    return f"Session does not exist for cron target {target}: {job.session_id}"


def _log_fire_save_failure(job_id: str, error: CronStorageError) -> None:
    _LOGGER.error(
        "Cron job state save failed after firing job=%s: %s",
        job_id,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


_LOGGER = get_logger("automation.cron")
