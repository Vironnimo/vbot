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
    ) -> list[CronOccurrence]:
        window_start = _as_utc(window_start_utc)
        window_end = _as_utc(window_end_utc)
        if window_end <= window_start:
            raise CronJobValidationError("projection window end must be after its start")
        occurrences: list[CronOccurrence] = []
        for job in self.jobs:
            if job.status != "active" or job.remaining_runs == 0:
                continue
            if job.schedule_type != "event":
                occurrences.extend(
                    schedule._project_job_occurrences(
                        self.timezone, job, window_start, window_end, self.now, max_per_job
                    )
                )
                continue
            try:
                event = self.calendar.get_event(job.event_id or "")
            except CalendarEventNotFoundError:
                continue
            occurrences.extend(
                CronOccurrence(
                    job_id=job.id,
                    name=job.name,
                    fire_at_utc=due.due_at,
                    schedule_type="event",
                    event_id=due.occurrence.event_id,
                    occurrence_id=due.occurrence.id,
                )
                for due in events.dues_in_window(
                    self.calendar, event, job, window_start, window_end, max_per_job
                )
            )
        occurrences.sort(key=lambda item: (item.fire_at_utc, item.job_id))
        return occurrences
