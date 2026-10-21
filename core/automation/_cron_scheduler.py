"""Cron task lifecycle, due fires, durable claims and restarts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from core.automation import _cron_claims as _claims
from core.automation import _cron_events as _events
from core.automation import _cron_schedule as _schedule
from core.automation import _cron_timing as _timing
from core.automation._cron_execution import _POST_FIRE_SAVE_RETRY_SECONDS
from core.automation._cron_jobs import (
    _SCHEDULE_FIELDS,
    CronJob,
    CronJobValidationError,
    CronStorageError,
    ScheduleType,
    _parse_iso_datetime,
)
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.automation.cron import CronService


class CronScheduler:
    def __init__(self, owner: CronService) -> None:
        self._owner = owner

    def start(self) -> None:
        """Load jobs and start per-job scheduling tasks. Idempotent."""
        if self._owner._started:
            return

        try:
            self._owner._jobs = self._owner._load_jobs()
            self._owner._storage_load_error = None
        except CronStorageError as error:
            self._owner._degrade_invalid_storage(error)
            self._owner._started = True
            return
        self._owner._jobs_loaded = True
        self._owner._started = True
        needs_save = False
        once_claims_to_remove: list[str] = []
        held_job_ids: set[str] = set()

        for job in self._owner._jobs.values():
            if job.status != "active":
                continue
            if job.schedule_type == "once":
                assert job.run_at is not None
                try:
                    claimed_at = _claims.read(
                        self._owner._once_fire_claims_dir, job.id, run_at=job.run_at
                    )
                except CronStorageError as error:
                    # The job may already have fired. Never fire it from this
                    # process; the next start re-evaluates a readable claim.
                    _LOGGER.warning(
                        "Holding once job with unreadable fire claim (job=%s): %s",
                        job.id,
                        error,
                    )
                    held_job_ids.add(job.id)
                    continue
                if claimed_at is not None:
                    _LOGGER.warning(
                        "Marking claimed once job as completed (id=%s claimed_at=%s)",
                        job.id,
                        claimed_at,
                    )
                    job.status = "completed"
                    job.last_fired_at = claimed_at
                    job.last_outcome = "unknown"
                    job.last_error = "vBot restarted after this once job was claimed"
                    job.last_run_silent = False
                    needs_save = True
                    once_claims_to_remove.append(job.id)
                    continue
            if job.remaining_runs == 0:
                job.status = "completed"
                if job.last_outcome is None:
                    job.last_outcome = "unknown"
                    job.last_error = "vBot restarted after the final Run was admitted"
                    job.last_run_silent = False
                needs_save = True
                continue

        if needs_save:
            try:
                self._owner._save_jobs()
            except CronStorageError as error:
                # jobs.json was readable; a failed write is not invalid storage.
                # Reconciled state stays in memory (none of it is scheduled), and
                # claims remain so the next start reconciles them again.
                _LOGGER.error("Cron startup reconciliation could not be saved: %s", error)
            else:
                for job_id in once_claims_to_remove:
                    _claims.remove(self._owner._once_fire_claims_dir, job_id)
            self._owner._notify_changed()

        self._owner._webhooks.load_states(
            [job.id for job in self._owner._jobs.values() if job.schedule_type == "webhook"]
        )
        for job in self._owner._jobs.values():
            if job.status == "active" and job.id not in held_job_ids:
                self._start_job_task(job)

    def stop(self) -> None:
        """Cancel all running cron tasks. Idempotent."""
        if not self._owner._started and not self._owner._job_tasks:
            return

        for job_id in list(self._owner._job_tasks):
            self._cancel_job_task(job_id, force=True)
        self._owner._pending_restarts.clear()
        self._owner._started = False

    async def aclose(self) -> None:
        """Stop cron scheduling and await canceled job tasks and pending saves."""
        tasks = list(self._owner._job_tasks.values())
        self.stop()

        pending_tasks = [task for task in tasks if not task.done()]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        await self._owner._watcher.aclose()
        if self._owner._crash_saves:
            await asyncio.gather(*self._owner._crash_saves, return_exceptions=True)

    def _start_job_task(self, job: CronJob) -> None:
        """Create and track one asyncio task for an active cron job."""
        if job.status != "active" or job.remaining_runs == 0:
            return

        self._cancel_job_task(job.id)

        task: asyncio.Task[None]
        if job.schedule_type == "once":
            task = asyncio.create_task(self._run_once_job(job), name=f"cron-job:{job.id}:once")
        elif job.schedule_type == "event":
            task = asyncio.create_task(self._run_event_job(job), name=f"cron-job:{job.id}:event")
        elif job.schedule_type == "webhook":
            task = asyncio.create_task(
                self._run_webhook_job(job), name=f"cron-job:{job.id}:webhook"
            )
        else:
            task = asyncio.create_task(
                self._run_recurring_job(job, job.schedule_type),
                name=f"cron-job:{job.id}:{job.schedule_type}",
            )

        self._owner._job_tasks[job.id] = task

        def on_done(completed_task: asyncio.Task[None], job_id: str = job.id) -> None:
            self._on_job_task_done(job_id, completed_task)

        task.add_done_callback(on_done)

    def _cancel_job_task(self, job_id: str, *, force: bool = False) -> None:
        """Cancel and forget one tracked asyncio task if present."""
        if not force and job_id in self._owner._executing_jobs:
            self._owner._pending_restarts.add(job_id)
            return
        task = self._owner._job_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _run_recurring_job(self, job: CronJob, schedule_type: ScheduleType) -> None:
        """Fire a cron or interval job at each due instant, catching up missed fires.

        A fire that came due while vBot did not run the job (offline, asleep)
        starts once, late, for the most recent due instant. Due instants that
        pass while the job's own Run is still running are skipped. A watching
        job checks first and starts its Run only when the output changed.
        """
        while True:
            current = self._owner._jobs.get(job.id)
            if (
                current is None
                or current.status != "active"
                or current.schedule_type != schedule_type
            ):
                return
            if schedule_type == "cron" and current.cron_expression is None:
                raise CronJobValidationError(
                    f"Cron job {current.id} is missing cron_expression while active"
                )

            owed = _schedule.owed_fire(self._owner._timezone, _timing._utc_now(), current)
            if owed is None:
                next_fire_at = self._owner.next_fire_at(current)
                if next_fire_at is None:
                    return
                due_at = _parse_iso_datetime(
                    next_fire_at, field_name="next_fire_at", allow_naive=False
                )
                # Only wall-clock cron schedules move when the timezone changes.
                reached = await _timing._sleep_until_utc(
                    due_at,
                    wake_event=self._owner._timezone_changed if schedule_type == "cron" else None,
                )
                if not reached:
                    continue
                current = self._owner._jobs.get(job.id)
                if (
                    current is None
                    or current.status != "active"
                    or current.schedule_type != schedule_type
                ):
                    return
                # After a long sleep of the computer, later due instants may have passed too.
                owed = _schedule.owed_fire(
                    self._owner._timezone, _timing._utc_now(), current
                ) or _schedule.OwedFire(due_at=due_at)

            if current.watch is not None:
                await self._owner._watcher.fire(current)
            else:
                await self._owner._execution._trigger_job_run(current, owed=owed)
            if job.id in self._owner._pending_restarts:
                return

    async def _run_event_job(self, job: CronJob) -> None:
        """Fire an event job once for each occurrence of its event, as each comes due.

        An occurrence that came due while vBot did not run the job starts late
        while its catch-up window is open (``_cron_events``); several such
        occurrences each start their own Run, earliest first. Starting a Run
        uses the occurrence even when the Run fails. A calendar change wakes
        the job: moved occurrences come due at their new time, removed ones
        never, and an event that is gone leaves the job waiting.
        """
        # Nothing due through this instant is owed until the calendar changes:
        # catch-up windows only close as time passes.
        settled: datetime | None = None
        while True:
            current = self._owner._jobs.get(job.id)
            if current is None or current.status != "active" or current.schedule_type != "event":
                return
            wake = self._owner._calendar_changed
            now = _timing._utc_now()
            event = self._owner._event_binding._bound_event(current)
            owed = (
                None
                if event is None or self._owner._calendar is None
                else _events.owed_occurrence(
                    self._owner._calendar, event, current, now, after=settled
                )
            )
            if owed is not None:
                await self._owner._execution._trigger_job_run(
                    current,
                    event_due=owed,
                )
                # An event edit during admission may move an open occurrence
                # before the last scan, including after a withdrawn claim.
                settled = None
                if job.id in self._owner._pending_restarts:
                    return
                continue
            settled = now
            upcoming = (
                None
                if event is None or self._owner._calendar is None
                else _events.next_due(self._owner._calendar, event, current, now)
            )
            reached = await _timing._sleep_until_utc(
                _NO_DUE if upcoming is None else upcoming.due_at, wake_event=wake
            )
            if not reached:
                settled = None

    async def _run_webhook_job(self, job: CronJob) -> None:
        """Start a Run whenever requests to the job's webhook wait, one Run at a time.

        Requests that arrive while a Run is active wait and start one follow-up
        Run together. A Run that could not start leaves its requests waiting and
        is tried again after the once-job backoff; a target that stays missing
        counts toward the failure stop.
        """
        webhooks = self._owner._webhooks
        failed_attempts = 0
        while True:
            current = self._owner._jobs.get(job.id)
            if current is None or current.status != "active" or current.schedule_type != "webhook":
                return
            wake = webhooks.wake(job.id)
            wake.clear()
            try:
                if not await webhooks.waiting(job.id):
                    await wake.wait()
                    continue
                outcome = await self._owner._execution._trigger_job_run(current, webhook=True)
                if job.id in self._owner._pending_restarts:
                    return
                if outcome.admitted or not await webhooks.waiting(job.id):
                    failed_attempts = 0
                    continue
                failed_attempts += 1
                await _timing._sleep(_timing._once_retry_delay(failed_attempts))
            except CronStorageError:
                failed_attempts += 1
                await _timing._sleep(_timing._once_retry_delay(failed_attempts))

    async def _run_once_job(self, job: CronJob) -> None:
        """Sleep until run_at, fire once, then mark completed.

        A failed fire (claim write or trigger error) is retried with bounded
        exponential backoff. Once the attempt limit is reached the job is
        abandoned (marked failed) and logged, so a permanently failing once
        job stops retrying instead of looping forever (e.g. its agent was
        deleted, leaving every trigger attempt to fail).
        """
        failed_fire_attempts = 0
        while True:
            current = self._owner._jobs.get(job.id)
            if current is None or current.status != "active" or current.schedule_type != "once":
                return

            run_at_utc = _schedule._parse_run_at_utc(self._owner._timezone, current)
            await _timing._sleep_until_utc(run_at_utc)

            latest = self._owner._jobs.get(job.id)
            if latest is None or latest.status != "active" or latest.schedule_type != "once":
                return
            claimed_at = _timing._utc_now_iso()
            try:
                await self._claim_once_fire(latest, claimed_at)
            except CronStorageError as error:
                _LOGGER.error(
                    "Cron once job fire claim failed for job=%s: %s",
                    latest.id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                if job.id in self._owner._pending_restarts:
                    return
                failed_fire_attempts += 1
                if await self._back_off_or_abandon_once_job(job.id, failed_fire_attempts):
                    return
                continue

            if job.id in self._owner._pending_restarts:
                await self._remove_claim(job.id)
                return
            outcome = await self._owner._execution._trigger_job_run(
                latest, owed=_schedule.OwedFire(due_at=run_at_utc)
            )
            if outcome.admitted:
                # Even an unsuccessful admitted Run consumes this fire. Keep
                # edits behind its final save and claim removal so a replacement
                # task cannot create a new claim that this one would remove.
                self._owner._executing_jobs.add(job.id)
                try:
                    latest = self._owner._jobs.get(job.id)
                    if latest is not None:
                        if (
                            outcome.succeeded
                            and latest.status == "active"
                            and job.id not in self._owner._pending_restarts
                        ):
                            latest.status = "completed"
                            self._owner._jobs[latest.id] = latest
                        if await self._owner._execution._persist_after_fire(latest.id):
                            await self._remove_claim(latest.id)
                finally:
                    self._owner._executing_jobs.discard(job.id)
                return

            # No Run was admitted: withdrawing this claim cannot repeat work.
            await self._remove_claim(job.id)
            if job.id in self._owner._pending_restarts:
                return
            current_after_failure = self._owner._jobs.get(job.id)
            if (
                current_after_failure is None
                or current_after_failure.remaining_runs == 0
                or current_after_failure.status != "active"
            ):
                return
            failed_fire_attempts += 1
            if await self._back_off_or_abandon_once_job(job.id, failed_fire_attempts):
                return

    async def _claim_once_fire(self, job: CronJob, claimed_at: str) -> None:
        # A reschedule must wait for this claim to settle before replacing its
        # task; otherwise the old task could leave or remove the new fire's claim.
        self._owner._executing_jobs.add(job.id)
        try:
            await self._owner._writer.call_async(
                partial(_claims.write, self._owner._once_fire_claims_dir, job, claimed_at)
            )
        except asyncio.CancelledError:
            # The ordered writer has settled, and admission has not started.
            await self._remove_claim(job.id)
            raise
        finally:
            self._owner._executing_jobs.discard(job.id)

    async def _remove_claim(self, job_id: str) -> None:
        await self._owner._writer.call_async(
            partial(_claims.remove, self._owner._once_fire_claims_dir, job_id)
        )

    async def _back_off_or_abandon_once_job(self, job_id: str, attempts: int) -> bool:
        """Wait out the backoff for a failed once fire, or abandon after the cap.

        Returns True when the job has been abandoned (marked failed) and the
        caller must stop; False after sleeping the backoff delay so the caller
        can retry the fire.
        """
        if attempts >= _ONCE_MAX_FIRE_ATTEMPTS:
            await self._abandon_once_job(job_id, attempts)
            return True

        await _timing._sleep(_timing._once_retry_delay(attempts))
        return False

    async def _abandon_once_job(self, job_id: str, attempts: int) -> None:
        """Mark a permanently failing once job failed so it stops retrying.

        The terminal ``failed`` status keeps the never-fired job visible and
        distinct from a successful ``completed`` fire; ``last_fired_at`` stays
        unset because the job never actually ran.
        """
        job = self._owner._jobs.get(job_id)
        if job is None or job.schedule_type != "once":
            return

        _LOGGER.error(
            "Abandoning once job after %d failed fire attempts (id=%s)",
            attempts,
            job_id,
        )
        baseline = self._owner._execution._failure_baseline(job_id)
        job.status = "failed"
        self._owner._jobs[job_id] = job
        await self._owner._execution._save_and_alert(job_id, baseline)
        await self._remove_claim(job_id)

    def _owed_event_now(self, job: CronJob) -> _events.EventDue | None:
        """Re-read an event job's next owed occurrence at the current wall clock."""
        event = self._owner._event_binding._bound_event(job)
        if event is None or self._owner._calendar is None:
            return None
        return _events.owed_occurrence(self._owner._calendar, event, job, _timing._utc_now())

    async def _prepare_event_fire(self, job: CronJob) -> tuple[CronJob, _events.EventDue] | None:
        """Claim a current occurrence, withdrawing it if it changes while the claim saves.

        The caller already holds a Run slot. The selection made before waiting
        for that slot may have moved, disappeared or expired. A persisted claim
        still precedes admission, but a withdrawn claim restores the previous
        cursor, including any other occurrences already consumed at that time.
        """
        due = self._owed_event_now(job)
        if due is None:
            return None
        before = self._owner._clone_job(job)
        job.last_attempt_at = _timing._utc_now_iso()
        job.last_error = None
        _events.consume(job, due)
        claimed = self._owner._clone_job(job)
        ready: tuple[CronJob, _events.EventDue] | None = None
        saved = False
        try:
            saved = await settle_before_cancelling(
                self._owner._execution._save_jobs_after_fire(job.id)
            )
            latest = self._owner._jobs.get(job.id)
            if (
                saved
                and latest is not None
                and latest.status == "active"
                and job.id not in self._owner._pending_restarts
                and all(getattr(latest, name) == getattr(before, name) for name in _SCHEDULE_FIELDS)
            ):
                unclaimed = self._owner._clone_job(latest)
                unclaimed.covered_until = before.covered_until
                unclaimed.event_progress = before.event_progress
                current = self._owed_event_now(unclaimed)
                if (
                    current is not None
                    and current.occurrence.id == due.occurrence.id
                    and current.due_at == due.due_at
                ):
                    # Text or location edits keep the firing but refresh its context.
                    ready = latest, current
        finally:
            if ready is None:
                await settle_before_cancelling(self._withdraw_event_fire(before, claimed))
        if not saved:
            # A failed claim must neither start a Run nor spin on the same owed occurrence.
            await _timing._sleep(_POST_FIRE_SAVE_RETRY_SECONDS)
        return ready

    async def _withdraw_event_fire(self, before: CronJob, claimed: CronJob) -> None:
        """Take back only this unadmitted claim, preserving any newer scheduling edit."""
        latest = self._owner._jobs.get(before.id)
        if latest is None or (latest.covered_until, latest.event_progress) != (
            claimed.covered_until,
            claimed.event_progress,
        ):
            return
        latest.covered_until = before.covered_until
        latest.event_progress = before.event_progress
        if latest.last_attempt_at == claimed.last_attempt_at:
            latest.last_attempt_at = before.last_attempt_at
            latest.last_error = before.last_error
        await self._owner._execution._persist_after_fire(latest.id)

    def _track_crash_save(self, save: Awaitable[bool]) -> None:
        task = asyncio.ensure_future(save)
        self._owner._crash_saves.add(task)
        task.add_done_callback(self._owner._crash_saves.discard)

    def _restart_job_task(self, job: CronJob) -> None:
        if job.id in self._owner._executing_jobs:
            self._owner._pending_restarts.add(job.id)
            return
        self._cancel_job_task(job.id)
        if job.status != "active":
            return
        self._start_job_task(job)

    def _on_job_task_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        owns_job_slot = self._owner._job_tasks.get(job_id) is task
        if owns_job_slot:
            self._owner._job_tasks.pop(job_id, None)

        if owns_job_slot and job_id in self._owner._pending_restarts:
            self._owner._pending_restarts.discard(job_id)
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    _LOGGER.error(
                        "Cron execution failed during reschedule (job=%s)",
                        job_id,
                        exc_info=(type(error), error, error.__traceback__),
                    )
            job = self._owner._jobs.get(job_id)
            if self._owner._started and job is not None and job.status == "active":
                self._start_job_task(job)
            return
        if task.cancelled():
            return

        error = task.exception()
        if error is None:
            return

        _LOGGER.error(
            "Cron job task failed for job=%s: %s",
            job_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        if not owns_job_slot:
            return

        baseline = self._owner._execution._failure_baseline(job_id)
        job = self._owner._jobs.get(job_id)
        if job is not None and job.status == "active" and job.schedule_type == "once":
            job.status = "failed"
            self._owner._jobs[job_id] = job
        if self._owner._execution._note_run_failure(job_id, error):
            # A crashed job task leaves only this callback, which must not wait for the file.
            self._track_crash_save(self._owner._execution._save_and_alert(job_id, baseline))

        job = self._owner._jobs.get(job_id)
        if self._owner._started and job is not None and job.status == "active":
            self._start_job_task(job)


_ONCE_MAX_FIRE_ATTEMPTS = 5


# An event job with no occurrence ahead waits for a calendar change.
_NO_DUE = datetime.max.replace(tzinfo=UTC)


_LOGGER = get_logger("automation.cron")
