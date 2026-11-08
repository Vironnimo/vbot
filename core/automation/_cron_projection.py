"""Detached, read-only Cron projection for calendar windows."""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from core.automation import _cron_events as events
from core.automation import _cron_schedule as schedule
from core.automation._cron_jobs import (
    MAX_PROJECTED_OCCURRENCES_PER_JOB,
    CronJob,
    CronJobValidationError,
    CronOccurrence,
    _as_utc,
)
from core.calendar import CalendarProjection
from core.calendar.errors import CalendarEventNotFoundError

MAX_PREVIEW_STEPS = 8192
MAX_PREVIEW_STEPS_PER_JOB = 1024


@dataclass(frozen=True, slots=True)
class CronPreview:
    """Proven job times in one window, with an explicit bounded-preview status."""

    occurrences: list[CronOccurrence]
    incomplete: bool


class _PreviewLimitError(Exception):
    """No further recurrence candidate may be expanded in this preview."""


@dataclass(slots=True)
class _PreviewWork:
    remaining: int
    job_remaining: int = 0

    def step(self) -> None:
        if self.remaining == 0 or self.job_remaining == 0:
            raise _PreviewLimitError
        self.remaining -= 1
        self.job_remaining -= 1


@dataclass(frozen=True, slots=True)
class CronProjection:
    """Copied jobs, events, clock and timezone captured in one loop turn."""

    jobs: tuple[CronJob, ...]
    timezone: ZoneInfo
    now: datetime
    calendar: CalendarProjection

    def project_occurrences(
        self,
        window_start_utc: datetime,
        window_end_utc: datetime,
        *,
        max_per_job: int = MAX_PROJECTED_OCCURRENCES_PER_JOB,
    ) -> CronPreview:
        window_start = _as_utc(window_start_utc)
        window_end = _as_utc(window_end_utc)
        if window_end <= window_start:
            raise CronJobValidationError("projection window end must be after its start")
        if isinstance(max_per_job, bool) or not isinstance(max_per_job, int) or max_per_job < 1:
            raise CronJobValidationError("max_per_job must be a positive integer")
        occurrences: list[CronOccurrence] = []
        incomplete = False
        work = _PreviewWork(MAX_PREVIEW_STEPS)
        for job in self.jobs:
            if job.status != "active" or job.remaining_runs == 0:
                continue
            work.job_remaining = MAX_PREVIEW_STEPS_PER_JOB
            if job.schedule_type != "event":
                candidates = schedule._project_job_occurrences(
                    self.timezone, job, window_start, window_end, self.now, work.step
                )
            else:
                try:
                    event = self.calendar.get_event(job.event_id or "")
                except CalendarEventNotFoundError:
                    continue
                candidates = (
                    CronOccurrence(
                        job_id=job.id,
                        name=job.name,
                        fire_at_utc=due.due_at,
                        schedule_type="event",
                        event_id=due.occurrence.event_id,
                        occurrence_id=due.occurrence.id,
                    )
                    for due in events.dues_in_window(
                        self.calendar, event, job, window_start, window_end, work.step
                    )
                )
            count = 0
            try:
                for occurrence in candidates:
                    if count == max_per_job:
                        incomplete = True
                        break
                    occurrences.append(occurrence)
                    count += 1
            except _PreviewLimitError:
                incomplete = True
        occurrences.sort(key=lambda item: (item.fire_at_utc, item.job_id))
        return CronPreview(occurrences, incomplete)
