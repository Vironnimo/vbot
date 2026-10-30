"""Detached Calendar read projections; no storage, callbacks or mutation authority."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from heapq import merge
from itertools import islice, takewhile
from zoneinfo import ZoneInfo

from core.calendar._events import (
    FIND_FREE_MAX_RESULTS,
    FIND_FREE_ROUNDING_MINUTES,
    MAX_DURATION_MINUTES,
    MAX_OCCURRENCES_PER_EVENT,
    CalendarEvent,
    EventOccurrence,
    FreeSlot,
    _clone_event,
    parse_occurrence_id,
)
from core.calendar._expansion import (
    iter_occurrences,
    occurrence_at,
    single_occurrence,
    window_occurrences,
)
from core.calendar._time import (
    _as_utc,
    _parse_iso_datetime,
    _round_up_to_minutes,
)
from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarValidationError,
)
from core.calendar.recurrence import (
    parse_date_string,
)
from core.calendar.when import looks_like_date


class CalendarProjection:
    """A private copy of committed events and their application timezone.

    A projection has no shared mutable event records with its source service.
    It can be expanded in a worker while the live catalog changes on the loop.
    """

    def __init__(self, events: list[CalendarEvent], timezone: ZoneInfo) -> None:
        self._events = {event.id: _clone_event(event) for event in events}
        self._timezone = timezone

    def _ensure_events_loaded(self, *, allow_degraded: bool = False) -> None:
        """A detached projection is already fully loaded."""

    def system_timezone_name(self) -> str:
        """Return the calendar's configured canonical IANA timezone name."""
        return str(self._timezone)

    def list_events(self) -> list[CalendarEvent]:
        """List all persisted events in stable created-order."""
        self._ensure_events_loaded(allow_degraded=True)
        ordered = sorted(self._events.values(), key=lambda item: (item.created_at, item.id))
        return [_clone_event(event) for event in ordered]

    def get_event(self, event_id: str) -> CalendarEvent:
        """Get one event by id."""
        self._ensure_events_loaded()
        return _clone_event(self._event(event_id))

    def get_occurrence(self, occurrence_id: str) -> EventOccurrence:
        """Get one occurrence by its id; a single event's id names its one occurrence."""
        self._ensure_events_loaded()
        event, key = self._occurrence_target(occurrence_id)
        if key is None:
            return single_occurrence(event, self._timezone)
        return self._occurrence(event, key)

    def occurrences_in_window(
        self,
        window_start_utc: datetime,
        window_end_utc: datetime,
        *,
        max_per_event: int = MAX_OCCURRENCES_PER_EVENT,
    ) -> list[EventOccurrence]:
        """Expand all events into occurrences overlapping the half-open window."""
        if (
            isinstance(max_per_event, bool)
            or not isinstance(max_per_event, int)
            or max_per_event < 1
        ):
            raise CalendarValidationError("max_per_event must be a positive integer")
        self._ensure_events_loaded(allow_degraded=True)
        window_start, window_end = _checked_window(window_start_utc, window_end_utc)
        occurrences: list[EventOccurrence] = []
        for event in self._events.values():
            occurrences.extend(
                window_occurrences(event, window_start, window_end, self._timezone, max_per_event)
            )
        occurrences.sort(key=lambda item: (item.start_utc, item.event_id, item.id))
        return occurrences

    def iter_occurrences(self, event: CalendarEvent, after: datetime) -> Iterator[EventOccurrence]:
        """Yield the occurrences of ``event`` that end after ``after``, in start order.

        Lazy: a repeating event without an end yields without end, so callers stop
        when they have what they need.
        """
        return iter_occurrences(event, _as_utc(after), self._timezone)

    def iter_window_occurrences(
        self, window_start_utc: datetime, window_end_utc: datetime
    ) -> Iterator[EventOccurrence]:
        """Stream every overlapping occurrence in stable start order, without list caps."""
        start, end = _checked_window(window_start_utc, window_end_utc)
        self._ensure_events_loaded()
        return merge(
            *(
                takewhile(lambda item: item.start_utc < end, self.iter_occurrences(event, start))
                for event in self._events.values()
            ),
            key=lambda item: (item.start_utc, item.event_id, item.id),
        )

    def find_free_slots(
        self,
        window_start_utc: datetime,
        window_end_utc: datetime,
        duration_minutes: int,
        *,
        max_results: int = FIND_FREE_MAX_RESULTS,
        now_utc: datetime | None = None,
    ) -> list[FreeSlot]:
        """Find the earliest free spans at least ``duration_minutes`` long in the window.

        Each slot is a whole gap between busy times, so callers see how much
        time is free rather than one duration-sized piece of it. Timed events
        block their span; all-day events block their whole local days. Slots
        start no earlier than the current time and on five-minute boundaries.
        """
        return list(
            islice(
                self.iter_free_slots(
                    window_start_utc, window_end_utc, duration_minutes, now_utc=now_utc
                ),
                max_results,
            )
        )

    def iter_free_slots(
        self,
        window_start_utc: datetime,
        window_end_utc: datetime,
        duration_minutes: int,
        *,
        now_utc: datetime | None = None,
    ) -> Iterator[FreeSlot]:
        """Stream whole free gaps, merging overlapping busy spans without materializing them."""
        if (
            isinstance(duration_minutes, bool)
            or not isinstance(duration_minutes, int)
            or duration_minutes <= 0
        ):
            raise CalendarValidationError("duration_minutes must be a positive integer")
        if duration_minutes > MAX_DURATION_MINUTES:
            raise CalendarValidationError(
                f"duration_minutes must not exceed {MAX_DURATION_MINUTES}"
            )
        window_start, window_end = _checked_window(window_start_utc, window_end_utc)
        self._ensure_events_loaded(allow_degraded=True)
        reference_now = _as_utc(now_utc) if now_utc is not None else datetime.now(UTC)
        duration = timedelta(minutes=duration_minutes)
        cursor = max(window_start, reference_now)
        cursor = _round_up_to_minutes(cursor, FIND_FREE_ROUNDING_MINUTES)
        for item in self.iter_window_occurrences(window_start, window_end):
            busy_start, busy_end = item.start_utc, min(item.end_utc, window_end)
            gap_end = min(busy_start, window_end)
            if cursor + duration <= gap_end:
                yield FreeSlot(start_utc=cursor, end_utc=gap_end)
            cursor = _round_up_to_minutes(max(cursor, busy_end), FIND_FREE_ROUNDING_MINUTES)
            if cursor >= window_end:
                return
        if cursor + duration <= window_end:
            yield FreeSlot(start_utc=cursor, end_utc=window_end)

    def parse_window_bound(self, value: str, *, is_end: bool) -> datetime:
        """Parse one window bound: a date (local day) or an ISO 8601 datetime."""
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            raise CalendarValidationError("window bounds must be non-empty strings")
        if looks_like_date(text):
            day = parse_date_string(text, field_name="window bound")
            local_midnight = datetime.combine(day, time.min).replace(tzinfo=self._timezone)
            if is_end:
                local_midnight += timedelta(days=1)
            return local_midnight.astimezone(UTC)
        parsed = _parse_iso_datetime(text, field_name="window bound")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._timezone)
        return parsed.astimezone(UTC)

    def parse_window(
        self, window_start_value: str, window_end_value: str
    ) -> tuple[datetime, datetime]:
        """Parse and validate a query window from agent-facing bound strings."""
        window_start = self.parse_window_bound(window_start_value, is_end=False)
        window_end = self.parse_window_bound(window_end_value, is_end=True)
        return _checked_window(window_start, window_end)

    def _event(self, event_id: str) -> CalendarEvent:
        event = self._events.get(event_id)
        if event is None:
            raise CalendarEventNotFoundError(f"Calendar event not found: {event_id}")
        return event

    def _occurrence_target(self, occurrence_id: str) -> tuple[CalendarEvent, str | None]:
        """The event an occurrence id names and its occurrence key (None for a single event)."""
        parsed = parse_occurrence_id(occurrence_id)
        if parsed is None:
            event = self._event(occurrence_id)
            if event.recurring:
                raise CalendarEventNotFoundError(
                    f"Calendar occurrence not found: {occurrence_id} is a repeating event"
                )
            return event, None
        event_id, key = parsed
        series = self._events.get(event_id)
        if series is None or not series.recurring:
            raise CalendarEventNotFoundError(f"Calendar occurrence not found: {occurrence_id}")
        return series, key

    def _occurrence(self, event: CalendarEvent, key: str) -> EventOccurrence:
        occurrence = occurrence_at(event, key, self._timezone)
        if occurrence is None:
            raise CalendarEventNotFoundError(
                f"Calendar occurrence not found: {event.id} has no occurrence at {key}"
            )
        return occurrence


def _checked_window(window_start: datetime, window_end: datetime) -> tuple[datetime, datetime]:
    start, end = _as_utc(window_start), _as_utc(window_end)
    if end <= start:
        raise CalendarValidationError("window end must be after its start")
    return start, end
