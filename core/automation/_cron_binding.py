"""Calendar-event binding changes and Cron target revalidation."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from core.automation import _cron_events as _events
from core.automation import _cron_timing as _timing
from core.automation._cron_jobs import (
    TERMINAL_CRON_JOB_STATUSES,
    CronJob,
    CronJobValidationError,
    CronStorageError,
)
from core.calendar import (
    BoundJob,
    CalendarEventNotFoundError,
    CalendarStorageError,
    EventJobTargetMissingError,
)
from core.utils.logging import get_logger
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.automation.cron import CronService
    from core.calendar import CalendarEvent


class CronEventBinding:
    def __init__(self, owner: CronService) -> None:
        self._owner = owner

    async def check_event_change(self, before: CalendarEvent, after: CalendarEvent) -> None:
        """Refuse an event change that would revive jobs into a target that is gone.

        A job that can no longer fire is history, so removing its Agent,
        Project or selected Session did not check it. When the change (for
        example, moving a past one-time event later) lets such a job fire
        again, its target is checked like a new job's; a target that exists but
        cannot run now is allowed, because it can recover. The Agent and
        Session reads run on the Session pool, and only for revived jobs.

        Raises:
            EventJobTargetMissingError: naming each revived job and what it misses.
        """
        try:
            self._owner._ensure_jobs_loaded()
        except CronStorageError:
            return  # Scheduling is disabled; nothing can fire.
        now = _timing._utc_now()
        revived = [
            self._owner._clone_job(job)
            for job in self._owner._jobs.values()
            if job.schedule_type == "event"
            and job.event_id == after.id
            and job.status not in TERMINAL_CRON_JOB_STATUSES
            and not self._event_job_can_fire(job, before, now)
            and self._event_job_can_fire(job, after, now)
        ]
        if not revived:
            return
        problems = (
            self._owner._missing_targets(revived)
            if self._owner._sessions is None
            else await self._owner._sessions.run_async(self._owner._missing_targets, revived)
        )
        if problems:
            raise EventJobTargetMissingError(problems)

    async def event_deleted(self, event_id: str, *, actor: str) -> tuple[BoundJob, ...]:
        """Delete the jobs bound to a deleted calendar event; return what was deleted."""
        async with self._owner._edits:
            try:
                self._owner._ensure_jobs_loaded()
            except CronStorageError:
                _LOGGER.error("Cron jobs of deleted calendar event %s cannot be read", event_id)
                raise
            removed = {
                job_id: job
                for job_id, job in self._owner._jobs.items()
                if job.schedule_type == "event" and job.event_id == event_id
            }
            if not removed:
                return ()
            for job_id in removed:
                del self._owner._jobs[job_id]
                self._owner._scheduler._cancel_job_task(job_id)

            def undo() -> None:
                self._owner._jobs.update(removed)
                for job in removed.values():
                    if self._owner._started and job.status == "active":
                        self._owner._scheduler._restart_job_task(job)

            await settle_before_cancelling(self._owner._save_edit(undo))
        ordered = sorted(removed.values(), key=lambda item: (item.created_at, item.id))
        for job in ordered:
            _LOGGER.info(
                "Cron job deleted with its calendar event (job=%s event=%s actor=%s)",
                job.id,
                event_id,
                actor,
            )
        return tuple(BoundJob(id=job.id, name=job.name) for job in ordered)

    def _on_calendar_changed(self) -> None:
        """Wake the event jobs, and tell listeners their next fires may have moved."""
        previous = self._owner._calendar_changed
        self._owner._calendar_changed = asyncio.Event()
        previous.set()
        if any(job.schedule_type == "event" for job in self._owner._jobs.values()):
            self._owner._notify_changed()

    def _event_of(self, job: CronJob) -> CalendarEvent | None:
        """The event of an event job; None when it is gone. Raises CalendarStorageError."""
        if self._owner._calendar is None or job.schedule_type != "event" or job.event_id is None:
            return None
        try:
            return self._owner._calendar.get_event(job.event_id)
        except CalendarEventNotFoundError:
            return None

    def _bound_event(self, job: CronJob) -> CalendarEvent | None:
        """:meth:`_event_of`, with an unreadable calendar treated as no event."""
        try:
            return self._event_of(job)
        except CalendarStorageError:
            return None

    def _event_job_can_fire(self, job: CronJob, event: CalendarEvent, now: datetime) -> bool:
        if self._owner._calendar is None:
            return False
        if job.id in self._owner._executing_jobs:
            return True
        return _events.can_fire(self._owner._calendar, event, job, now)

    def _check_event_binding(self, job: CronJob) -> None:
        """Refuse an event job whose calendar event does not exist."""
        if job.schedule_type != "event":
            return
        if self._owner._calendar is None:
            raise CronJobValidationError("Calendar events are not available for cron jobs")
        try:
            event = self._event_of(job)
        except CalendarStorageError as error:
            raise CronStorageError(f"The calendar cannot be read: {error}") from error
        if event is None:
            raise CronJobValidationError(f"Calendar event not found: {job.event_id}")


_LOGGER = get_logger("automation.cron")
