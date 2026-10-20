"""The context note before a Cron Run's input, read by the Agent as a System Reminder.

It says why the Run starts when that is not simply its due time (an event, a
late start, a change of what the job watches), where the final answer goes, and
carries the job's previous answer as data.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from zoneinfo import ZoneInfo

from core.automation import _cron_delivery as _delivery
from core.automation import _cron_events as _events
from core.automation import _cron_schedule as _schedule
from core.automation import _cron_timing as _timing
from core.automation import _cron_watch as _watch
from core.automation._cron_jobs import CronJob

# A fire that starts at most this late counts as on time and gets no late notice.
# Wall-clock waits recheck every minute.
ON_TIME_SECONDS = 60


def run_context(
    job: CronJob,
    timezone: ZoneInfo,
    *,
    late: LateFire | None = None,
    event_due: _events.EventDue | None = None,
    change: _watch.WatchChange | None = None,
) -> str:
    """The note before the Run's input.

    It names the event the job is due for, any lateness or the change that
    started the Run, says where the final answer goes, and carries the previous
    answer as data.
    """
    parts = []
    if event_due is not None:
        parts.append(_events.context_note(job, event_due))
    if late is not None:
        parts.append(late.notice(job.id, _timing._utc_now()))
    if change is not None:
        parts.append(_watch.change_note(change, timezone))
    parts.append(_delivery.delivery_note(job.delivery))
    if job.last_result is not None:
        parts.append(_delivery.previous_result_note(job.last_result, timezone))
    return "\n\n".join(parts)


def late_fire(owed: _schedule.OwedFire, timezone: ZoneInfo) -> LateFire | None:
    """Describe a fire that starts after it was due; ``None`` when it is on time."""
    now = _timing._utc_now()
    if (now - owed.due_at).total_seconds() <= ON_TIME_SECONDS and not owed.earlier_missed:
        return None
    return LateFire(owed=owed, noticed_at=now, timezone=timezone)


@dataclasses.dataclass(frozen=True, slots=True)
class LateFire:
    """A fire that starts after its due instant, or after earlier fires were missed."""

    owed: _schedule.OwedFire
    noticed_at: datetime
    timezone: ZoneInfo

    @property
    def late_by_seconds(self) -> int:
        return max(int((self.noticed_at - self.owed.due_at).total_seconds()), 0)

    def notice(self, job_id: str, starting_at: datetime) -> str:
        """The note before the Run's input that tells the Agent which fires it replaces."""
        due = self._local(self.owed.due_at)
        if self.late_by_seconds > ON_TIME_SECONDS:
            sentences = [
                f"Cron job {job_id} was due at {due} and is starting late, at "
                f"{self._local(starting_at)}."
            ]
        else:
            sentences = [f"Cron job {job_id} is starting at its due time, {due}."]
        earlier = self.owed.earlier_missed
        first = self.owed.first_due_at
        if earlier == 1 and first is not None:
            sentences.append(
                f"The earlier due time {self._local(first)} was missed and will not run separately."
            )
        elif earlier and first is not None:
            count = (
                f"At least {earlier}" if earlier >= _schedule.MAX_COUNTED_MISSED_FIRES else earlier
            )
            sentences.append(
                f"{count} earlier due times, the first at {self._local(first)}, were missed and "
                "will not run separately."
            )
        sentences.append(
            f"vBot did not run the job at {'those times' if earlier else 'the due time'}, for "
            "example because the server was off or the computer was asleep."
        )
        sentences.append(
            "Carry out the instruction that follows now, and adapt any part of it that depends "
            "on when it runs, such as a greeting, a deadline that has passed, or the period it "
            "covers."
        )
        sentences.append(
            "If the instruction no longer makes sense this late, say so instead of carrying it out."
        )
        return " ".join(sentences)

    def _local(self, instant: datetime) -> str:
        return instant.astimezone(self.timezone).replace(microsecond=0).isoformat()
