"""What the calendar asks the owner of the jobs that run at its events.

Jobs that run an instruction at a calendar event's occurrences belong to
another owner (Cron). The calendar does not run them; it only asks that owner
before an event change and tells it after an event is deleted, through
:class:`EventJobs`. The owner reads events and occurrences from the calendar
and follows its change notifications.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.calendar._events import CalendarEvent


@dataclass(frozen=True, slots=True)
class BoundJob:
    """A job that ran at an event's occurrences."""

    id: str
    name: str


class EventJobs(Protocol):
    """The owner of the jobs bound to calendar events."""

    async def check_event_change(self, before: CalendarEvent, after: CalendarEvent) -> None:
        """Refuse a change of ``before`` into ``after`` that its jobs cannot follow.

        Raises ``EventJobTargetMissingError`` when the change would let jobs run
        again whose target no longer exists.
        """
        ...

    async def event_deleted(self, event_id: str, *, actor: str) -> tuple[BoundJob, ...]:
        """Delete the jobs of the deleted event ``event_id`` and return them."""
        ...
