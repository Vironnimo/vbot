"""Calendar event catalog, occurrence changes, expansion and free-time search."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.calendar._events import (
    CALENDAR_EVENTS_FORMAT,
    DEFAULT_ALL_DAY_DURATION_DAYS,
    DEFAULT_EVENT_DURATION_MINUTES,
    FIND_FREE_MAX_RESULTS,
    FIND_FREE_ROUNDING_MINUTES,
    MAX_CALENDAR_EVENTS,
    MAX_DESCRIPTION_LENGTH,
    MAX_DURATION_DAYS,
    MAX_DURATION_MINUTES,
    MAX_EXDATES_PER_EVENT,
    MAX_LOCATION_LENGTH,
    MAX_OCCURRENCES_PER_EVENT,
    MAX_OVERRIDES_PER_EVENT,
    MAX_TITLE_LENGTH,
    MAX_WINDOW_DAYS,
    OVERRIDE_FIELDS,
    CalendarEvent,
    EventOccurrence,
    FreeSlot,
    _check_event,
    _check_span,
    _clone_event,
    _load_events_document,
    _optional_text,
    _read_event,
    _required_text,
    is_legacy_events_document,
    parse_occurrence_id,
    validate_calendar_events_data,
    validate_calendar_events_file,
)
from core.calendar._expansion import (
    iter_occurrences,
    local_time,
    occurrence_at,
    occurrence_key,
    occurrence_keys,
    single_occurrence,
    window_occurrences,
)
from core.calendar._time import (
    _as_utc,
    _default_timezone,
    _merge_intervals,
    _parse_iso_datetime,
    _resolve_zone,
    _round_up_to_minutes,
    _utc_now,
    _utc_now_iso,
)
from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarStorageError,
    CalendarValidationError,
)
from core.calendar.event_jobs import BoundJob, EventJobs
from core.calendar.recurrence import (
    FIRST_EVENT_YEAR,
    LAST_EVENT_YEAR,
    normalize_rrule,
    parse_date_string,
)
from core.calendar.when import looks_like_date, parse_when
from core.config_validation import JsonDiagnostic
from core.json_documents import (
    JsonDocumentWriteError,
    write_json_document,
)
from core.utils.file_status import exists_strict
from core.utils.ids import new_id
from core.utils.logging import get_logger

_LOGGER = get_logger("calendar.service")
# Who caused a mutation when the caller does not say (direct in-process callers).
_DEFAULT_ACTOR = "internal"
_EVENT_INPUT_FIELDS = frozenset(("title", "description", "location", "start", "end", "rrule"))
_DEFAULT_LENGTH = timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)
_DEFAULT_DAYS = timedelta(days=DEFAULT_ALL_DAY_DURATION_DAYS)
_ONE_DAY = timedelta(days=1)

__all__ = [
    "MAX_CALENDAR_EVENTS",
    "MAX_EXDATES_PER_EVENT",
    "MAX_OCCURRENCES_PER_EVENT",
    "MAX_WINDOW_DAYS",
    "MAX_TITLE_LENGTH",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_LOCATION_LENGTH",
    "MAX_DURATION_MINUTES",
    "MAX_DURATION_DAYS",
    "DEFAULT_EVENT_DURATION_MINUTES",
    "DEFAULT_ALL_DAY_DURATION_DAYS",
    "FIND_FREE_MAX_RESULTS",
    "FIND_FREE_ROUNDING_MINUTES",
    "EventOccurrence",
    "FreeSlot",
    "CalendarEvent",
    "validate_calendar_events_file",
    "validate_calendar_events_data",
    "CalendarService",
]


class CalendarService:
    """Manage persisted calendar events, their occurrences, expansion and free-slot search.

    Input times without an offset are local times of the server zone; times with
    an offset name their instant. A new timed event keeps its times in the
    server zone of its creation; an all-day event keeps dates and follows the
    server zone.

    Jobs that run at an event's occurrences belong to the owner bound with
    :meth:`bind_event_jobs`: it may refuse an event change and deletes an
    event's jobs with the event. Changes and deletions run one at a time.
    """

    def __init__(self, data_root: str | Path, *, tz: str | ZoneInfo | None = None) -> None:
        self._data_root = Path(data_root).expanduser()
        self._calendar_dir = self._data_root / "calendar"
        self._events_path = self._calendar_dir / "events.json"
        self._timezone = _resolve_zone(tz) if isinstance(tz, str) else (tz or _default_timezone())
        self._events: dict[str, CalendarEvent] = {}
        self._invalid_event_entries: list[Any] = []
        self._storage_load_error: CalendarStorageError | None = None
        self._events_loaded = False
        # The file holds the events of an earlier vBot version: the next save replaces it.
        self._replace_legacy_document = False
        self._changed_callbacks: set[Callable[[], None]] = set()
        self._event_jobs: EventJobs | None = None
        self._edits = asyncio.Lock()

    def bind_event_jobs(self, event_jobs: EventJobs) -> None:
        """Ask ``event_jobs`` before event changes and tell it about deleted events."""
        self._event_jobs = event_jobs

    def add_changed_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to persisted calendar changes and return an unsubscribe function."""
        self._changed_callbacks.add(callback)

        def unsubscribe() -> None:
            self._changed_callbacks.discard(callback)

        return unsubscribe

    def system_timezone_name(self) -> str:
        """Return the calendar's configured canonical IANA timezone name."""
        return str(self._timezone)

    def set_timezone(self, timezone_name: str) -> None:
        """Apply a new application timezone to future local-time operations."""
        timezone = _resolve_zone(timezone_name)
        if timezone == self._timezone:
            return
        self._timezone = timezone
        self._notify_changed()

    # -- events ---------------------------------------------------------------------------

    def create_event(
        self,
        *,
        title: str,
        start: str,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        rrule: str | None = None,
        actor: str = _DEFAULT_ACTOR,
    ) -> CalendarEvent:
        """Create and persist a new event.

        ``start`` is a date (an all-day event) or a time; ``end`` has the same
        form and is exclusive, one hour or one day after the start when omitted.
        An all-day end on the start day means that one day. ``rrule`` is an
        RFC 5545 RRULE.
        """
        self._ensure_events_loaded()
        if len(self._events) >= MAX_CALENDAR_EVENTS:
            raise CalendarValidationError(
                f"The calendar stores at most {MAX_CALENDAR_EVENTS} events; delete old ones first"
            )
        fields: dict[str, Any] = {"start": start}
        if end is not None:
            fields["end"] = end
        start_text, end_text, tz_name = self._event_times(fields, None)
        now = _utc_now_iso()
        event = CalendarEvent(
            id=new_id("evt", claim=lambda candidate: candidate not in self._events),
            title=_required_text(title, "title", MAX_TITLE_LENGTH),
            description=_optional_text(description, "description", MAX_DESCRIPTION_LENGTH),
            location=_optional_text(location, "location", MAX_LOCATION_LENGTH),
            start=start_text,
            end=end_text,
            tz_name=tz_name,
            rrule=_event_rule(rrule, start_text, tz_name),
            created_at=now,
            updated_at=now,
        )
        self._store(event, None)
        _LOGGER.info(
            "Calendar event created (event=%s recurring=%s actor=%s)",
            event.id,
            event.recurring,
            actor,
        )
        return _clone_event(event)

    def list_events(self) -> list[CalendarEvent]:
        """List all persisted events in stable created-order."""
        self._ensure_events_loaded(allow_degraded=True)
        ordered = sorted(self._events.values(), key=lambda item: (item.created_at, item.id))
        return [_clone_event(event) for event in ordered]

    def get_event(self, event_id: str) -> CalendarEvent:
        """Get one event by id."""
        self._ensure_events_loaded()
        return _clone_event(self._event(event_id))

    async def update_event(
        self, event_id: str, *, actor: str = _DEFAULT_ACTOR, **fields: Any
    ) -> CalendarEvent:
        """Change a whole event: title, description, location, start, end or rrule.

        Omitted fields keep their value. A new start alone keeps the event's
        length; an empty or null rrule stops the repetition. Moving a repeating
        event's start moves its removed and changed occurrences with it;
        occurrences the new rule no longer produces lose their changes.
        """
        self._ensure_events_loaded()
        self._event(event_id)
        unknown_fields = sorted(set(fields) - _EVENT_INPUT_FIELDS)
        if unknown_fields:
            raise CalendarValidationError(
                f"Unsupported calendar event fields: {', '.join(unknown_fields)}"
            )
        async with self._edits:
            event = self._event(event_id)
            candidate = self._updated_event(event, fields)
            changed = _changed_fields(event, candidate)
            if not changed:
                return _clone_event(event)
            candidate.updated_at = _utc_now_iso()
            await self._store_change(candidate, event)
        _LOGGER.info(
            "Calendar event updated (event=%s fields=%s actor=%s)",
            event_id,
            ",".join(changed),
            actor,
        )
        return _clone_event(candidate)

    async def delete_event(
        self, event_id: str, *, actor: str = _DEFAULT_ACTOR
    ) -> tuple[BoundJob, ...]:
        """Delete one event with all its occurrences and the jobs bound to it.

        Returns the deleted jobs. When they cannot be deleted, the event stays
        deleted and their owner's error is raised; such jobs no longer run.
        """
        async with self._edits:
            self._ensure_events_loaded()
            removed = self._event(event_id)
            del self._events[event_id]
            try:
                self._save_events()
            except Exception:
                self._events[event_id] = removed
                raise
            self._notify_changed()
            _LOGGER.info("Calendar event deleted (event=%s actor=%s)", event_id, actor)
            if self._event_jobs is None:
                return ()
            return await self._event_jobs.event_deleted(event_id, actor=actor)

    # -- occurrences ------------------------------------------------------------------------

    def get_occurrence(self, occurrence_id: str) -> EventOccurrence:
        """Get one occurrence by its id; a single event's id names its one occurrence."""
        self._ensure_events_loaded()
        event, key = self._occurrence_target(occurrence_id)
        if key is None:
            return single_occurrence(event, self._timezone)
        return self._occurrence(event, key)

    async def update_occurrence(
        self, occurrence_id: str, *, actor: str = _DEFAULT_ACTOR, **fields: Any
    ) -> EventOccurrence:
        """Change one occurrence of a repeating event: title, description, location, start, end.

        The rest of the series stays as it is. A new start alone keeps the
        occurrence's length; the occurrence keeps the event's kind (timed or
        all-day). A change back to the series' own value drops that change.
        """
        self._ensure_events_loaded()
        unknown_fields = sorted(set(fields) - OVERRIDE_FIELDS)
        if unknown_fields:
            raise CalendarValidationError(
                f"Unsupported calendar occurrence fields: {', '.join(unknown_fields)}"
            )
        async with self._edits:
            return await self._update_occurrence(occurrence_id, fields, actor)

    async def _update_occurrence(
        self, occurrence_id: str, fields: dict[str, Any], actor: str
    ) -> EventOccurrence:
        event, key = self._occurrence_target(occurrence_id)
        if key is None:
            raise CalendarEventNotFoundError(f"Calendar occurrence not found: {occurrence_id}")
        current = self._occurrence(event, key)
        override = dict(event.overrides.get(key, {}))
        if "title" in fields:
            override["title"] = _required_text(fields["title"], "title", MAX_TITLE_LENGTH)
        if "description" in fields:
            override["description"] = _optional_text(
                fields["description"], "description", MAX_DESCRIPTION_LENGTH
            )
        if "location" in fields:
            override["location"] = _optional_text(
                fields["location"], "location", MAX_LOCATION_LENGTH
            )
        if "start" in fields or "end" in fields:
            override["start"], override["end"] = self._occurrence_times(event, current, fields)
        override = _without_series_values(event, key, override)
        candidate = _clone_event(event)
        if override:
            candidate.overrides[key] = override
        else:
            candidate.overrides.pop(key, None)
        if candidate.overrides == event.overrides:
            return current
        if len(candidate.overrides) > MAX_OVERRIDES_PER_EVENT:
            raise CalendarValidationError(
                f"events allow at most {MAX_OVERRIDES_PER_EVENT} changed occurrences"
            )
        candidate.updated_at = _utc_now_iso()
        await self._store_change(candidate, event)
        _LOGGER.info(
            "Calendar occurrence updated (event=%s occurrence=%s fields=%s actor=%s)",
            event.id,
            key,
            ",".join(sorted(fields)),
            actor,
        )
        return self._occurrence(candidate, key)

    async def delete_occurrence(
        self, occurrence_id: str, *, actor: str = _DEFAULT_ACTOR
    ) -> EventOccurrence:
        """Remove one occurrence of a repeating event (an RFC 5545 EXDATE); return it.

        The rest of the series stays; the occurrence's changes go with it.
        """
        self._ensure_events_loaded()
        async with self._edits:
            event, key = self._occurrence_target(occurrence_id)
            if key is None:
                raise CalendarEventNotFoundError(f"Calendar occurrence not found: {occurrence_id}")
            removed = self._occurrence(event, key)
            if len(event.exdates) >= MAX_EXDATES_PER_EVENT:
                raise CalendarValidationError(
                    f"events allow at most {MAX_EXDATES_PER_EVENT} removed occurrences"
                )
            candidate = _clone_event(event)
            candidate.exdates = sorted([*event.exdates, key])
            candidate.overrides.pop(key, None)
            candidate.updated_at = _utc_now_iso()
            await self._store_change(candidate, event)
        _LOGGER.info(
            "Calendar occurrence removed (event=%s occurrence=%s actor=%s)", event.id, key, actor
        )
        return removed

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

    def event_occurrences(
        self, event: CalendarEvent, window_start: datetime, window_end: datetime
    ) -> list[EventOccurrence]:
        """The occurrences of one event overlapping the half-open UTC window."""
        return window_occurrences(
            event,
            _as_utc(window_start),
            _as_utc(window_end),
            self._timezone,
            MAX_OCCURRENCES_PER_EVENT,
        )

    def iter_occurrences(self, event: CalendarEvent, after: datetime) -> Iterator[EventOccurrence]:
        """Yield the occurrences of ``event`` that end after ``after``, in start order.

        Lazy: a repeating event without an end yields without end, so callers stop
        when they have what they need.
        """
        return iter_occurrences(event, _as_utc(after), self._timezone)

    def next_start(self, event: CalendarEvent, instant: datetime) -> datetime | None:
        """The UTC start of ``event``'s first occurrence at or after ``instant``, if any."""
        instant = _as_utc(instant)
        for occurrence in self.iter_occurrences(event, instant):
            if occurrence.start_utc >= instant:
                return occurrence.start_utc
        return None

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
        busy = [
            (max(item.start_utc, window_start), min(item.end_utc, window_end))
            for event in self._events.values()
            for item in window_occurrences(
                event, window_start, window_end, self._timezone, limit=None
            )
        ]
        merged = _merge_intervals(busy)
        cursor = max(window_start, reference_now)
        cursor = _round_up_to_minutes(cursor, FIND_FREE_ROUNDING_MINUTES)
        slots: list[FreeSlot] = []
        for busy_start, busy_end in merged:
            gap_end = min(busy_start, window_end)
            if cursor + duration <= gap_end and len(slots) < max_results:
                slots.append(FreeSlot(start_utc=cursor, end_utc=gap_end))
            cursor = _round_up_to_minutes(max(cursor, busy_end), FIND_FREE_ROUNDING_MINUTES)
            if cursor >= window_end or len(slots) >= max_results:
                break
        if len(slots) < max_results and cursor + duration <= window_end:
            slots.append(FreeSlot(start_utc=cursor, end_utc=window_end))
        return slots

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

    def resolve_when(
        self, value: str, *, now_utc: datetime | None = None
    ) -> tuple[datetime, datetime]:
        """Resolve one ``when`` expression (see core.calendar.when) to a UTC window."""
        return parse_when(value, now_utc=now_utc or _utc_now(), tz=self._timezone)

    # -- event times ----------------------------------------------------------------------

    def _updated_event(self, event: CalendarEvent, fields: dict[str, Any]) -> CalendarEvent:
        """The event with ``fields`` applied, validated; occurrence keys follow its start."""
        candidate = _clone_event(event)
        if "title" in fields:
            candidate.title = _required_text(fields["title"], "title", MAX_TITLE_LENGTH)
        if "description" in fields:
            candidate.description = _optional_text(
                fields["description"], "description", MAX_DESCRIPTION_LENGTH
            )
        if "location" in fields:
            candidate.location = _optional_text(fields["location"], "location", MAX_LOCATION_LENGTH)
        if "start" in fields or "end" in fields:
            candidate.start, candidate.end, candidate.tz_name = self._event_times(fields, event)
        if "rrule" in fields:
            candidate.rrule = _event_rule(fields["rrule"], candidate.start, candidate.tz_name)
        elif candidate.rrule is not None and (
            candidate.start != event.start or candidate.tz_name != event.tz_name
        ):
            # The rule's meaning depends on the start (its weekday, UNTIL, BYHOUR).
            candidate.rrule = _event_rule(candidate.rrule, candidate.start, candidate.tz_name)
        if candidate.rrule is None or candidate.all_day != event.all_day:
            candidate.exdates, candidate.overrides = [], {}
        elif (candidate.start, candidate.rrule) != (event.start, event.rrule):
            _move_occurrence_keys(candidate, event)
        return candidate

    def _event_times(
        self, fields: dict[str, Any], event: CalendarEvent | None
    ) -> tuple[str, str, str | None]:
        """Stored start, end and zone of an event from the start and end a call sends."""
        if "start" in fields:
            start, zone = self._input_time(fields["start"], "start", event)
        else:
            assert event is not None
            start = local_time(event.start)
            zone = None if event.tz_name is None else _resolve_zone(event.tz_name)
        all_day = zone is None
        if fields.get("end") is not None:
            end, end_zone = self._input_time(fields["end"], "end", event)
            if (end_zone is None) != all_day:
                form = "a date" if all_day else "a date-time"
                raise CalendarValidationError(f"end must be {form} like the start")
        elif event is not None and event.all_day == all_day:
            # A moved event keeps its length; an unmoved one its end.
            end = start + (local_time(event.end) - local_time(event.start))
        else:
            end = start + (_DEFAULT_DAYS if all_day else _DEFAULT_LENGTH)
        _check_span(start, end, all_day=all_day)
        if all_day and end == start:
            end = start + _ONE_DAY
        return (
            occurrence_key(start, all_day=all_day),
            occurrence_key(end, all_day=all_day),
            None if zone is None else str(zone),
        )

    def _occurrence_times(
        self, event: CalendarEvent, current: EventOccurrence, fields: dict[str, Any]
    ) -> tuple[str, str]:
        """Stored start and end of a changed occurrence; it keeps the event's kind."""
        own_start = local_time(current.start)
        length = local_time(current.end) - own_start
        start = own_start
        if fields.get("start") is not None:
            start, _ = self._input_time(fields["start"], "start", event, kind_of=event)
        if fields.get("end") is not None:
            end, _ = self._input_time(fields["end"], "end", event, kind_of=event)
        else:
            end = start + length
        _check_span(start, end, all_day=event.all_day)
        if event.all_day and end == start:
            end = start + _ONE_DAY
        return (
            occurrence_key(start, all_day=event.all_day),
            occurrence_key(end, all_day=event.all_day),
        )

    def _input_time(
        self,
        value: object,
        field_name: str,
        event: CalendarEvent | None,
        *,
        kind_of: CalendarEvent | None = None,
    ) -> tuple[datetime, ZoneInfo | None]:
        """A sent start or end as a naive local time and its zone (None for a date).

        A time without an offset is a server-local time; it is kept in the
        zone of a timed ``event``, else in the server zone. With ``kind_of``,
        the value must have that event's kind.
        """
        if not isinstance(value, str) or not value.strip():
            raise CalendarValidationError(
                f"{field_name} must be a date (YYYY-MM-DD) or an ISO 8601 datetime"
            )
        text = value.strip()
        if looks_like_date(text):
            day = parse_date_string(text, field_name=field_name)
            if kind_of is not None and not kind_of.all_day:
                raise CalendarValidationError(
                    f"{field_name} must be a date-time: an occurrence keeps the event's kind"
                )
            _check_year(day.year, field_name)
            return datetime.combine(day, time.min), None
        if kind_of is not None and kind_of.all_day:
            raise CalendarValidationError(
                f"{field_name} must be a date: an occurrence keeps the event's kind"
            )
        parsed = _parse_iso_datetime(text, field_name=field_name)
        zone = (
            _resolve_zone(event.tz_name)
            if event is not None and event.tz_name is not None
            else self._timezone
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._timezone)
        local = parsed.astimezone(zone).replace(tzinfo=None, microsecond=0)
        _check_year(local.year, field_name)
        return local, zone

    # -- storage --------------------------------------------------------------------------

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

    async def _store_change(self, candidate: CalendarEvent, previous: CalendarEvent) -> None:
        """:meth:`_store` a changed event once the owner of its jobs accepts the change."""
        _check_event(candidate)
        if self._event_jobs is not None:
            await self._event_jobs.check_event_change(
                _clone_event(previous), _clone_event(candidate)
            )
        self._store(candidate, previous)

    def _store(self, candidate: CalendarEvent, previous: CalendarEvent | None) -> None:
        """Validate ``candidate``, persist it in place of ``previous`` and announce the change."""
        _check_event(candidate)
        self._events[candidate.id] = candidate
        try:
            self._save_events()
        except Exception:
            if previous is None:
                self._events.pop(candidate.id, None)
            else:
                self._events[candidate.id] = previous
            raise
        self._notify_changed()

    def _load_events(self) -> dict[str, CalendarEvent]:
        self._ensure_storage_exists()
        document = _load_events_document(self._events_path) or {"events": []}
        self._invalid_event_entries = []
        if is_legacy_events_document(document):
            ignored = document.get("events")
            _LOGGER.warning(
                "Ignoring the calendar events of an earlier vBot version; the next change "
                "replaces them (events=%s)",
                len(ignored) if isinstance(ignored, list) else 0,
            )
            self._replace_legacy_document = True
            return {}
        events: dict[str, CalendarEvent] = {}
        for index, item in enumerate(document["events"]):
            diagnostics: list[JsonDiagnostic] = []
            event = _read_event(diagnostics, f"$.events[{index}]", item)
            if event is None:
                details = "; ".join(
                    f"{diagnostic.path}: {diagnostic.message}"
                    for diagnostic in diagnostics
                    if diagnostic.severity == "error"
                )
                _LOGGER.warning("Skipping invalid calendar event: %s", details)
                self._invalid_event_entries.append(item)
                continue
            if event.id in events:
                _LOGGER.warning(
                    "Skipping duplicate calendar event id at $.events[%d]: %s", index, event.id
                )
                self._invalid_event_entries.append(item)
                continue
            events[event.id] = event
        return events

    def _save_events(self) -> None:
        """Write the events, invalid entries verbatim, and unknown fields back.

        A file that no longer loads is never overwritten; one of an earlier vBot
        version is replaced.
        """
        self._ensure_storage_exists()
        events = [
            event.to_dict()
            for event in sorted(self._events.values(), key=lambda item: (item.created_at, item.id))
        ] + list(self._invalid_event_entries)
        try:
            write_json_document(
                self._events_path,
                {"events": events},
                CALENDAR_EVENTS_FORMAT,
                reset=self._replace_legacy_document,
            )
        except JsonDocumentWriteError as error:
            raise CalendarStorageError(str(error)) from error
        except OSError as error:
            raise CalendarStorageError(f"Cannot write {self._events_path}: {error}") from error
        self._replace_legacy_document = False

    def _notify_changed(self) -> None:
        for callback in tuple(self._changed_callbacks):
            try:
                callback()
            except Exception as error:
                _LOGGER.error(
                    "Calendar change callback failed: %s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

    def _ensure_events_loaded(self, *, allow_degraded: bool = False) -> None:
        if self._events_loaded:
            if self._storage_load_error is not None and not allow_degraded:
                raise CalendarStorageError(str(self._storage_load_error))
            return
        try:
            self._events = self._load_events()
            self._storage_load_error = None
            self._events_loaded = True
        except CalendarStorageError as error:
            self._degrade_invalid_storage(error)
            if not allow_degraded:
                raise CalendarStorageError(str(error)) from error

    def _degrade_invalid_storage(self, error: CalendarStorageError) -> None:
        """Keep the runtime available while preventing writes over unreadable data."""
        self._events = {}
        self._invalid_event_entries = []
        self._storage_load_error = error
        self._events_loaded = True
        _LOGGER.error("Calendar storage is invalid; mutations are disabled: %s", error)

    def _ensure_storage_exists(self) -> None:
        try:
            self._calendar_dir.mkdir(parents=True, exist_ok=True)
            if not exists_strict(self._events_path):
                write_json_document(self._events_path, {"events": []}, CALENDAR_EVENTS_FORMAT)
        except OSError as error:
            raise CalendarStorageError(
                f"Cannot initialize calendar storage at {self._calendar_dir}: {error}"
            ) from error


def _event_rule(value: object, start: str, tz_name: str | None) -> str | None:
    zone = None if tz_name is None else _resolve_zone(tz_name)
    return normalize_rrule(value, start=local_time(start), zone=zone)


def _move_occurrence_keys(candidate: CalendarEvent, event: CalendarEvent) -> None:
    """Move removed and changed occurrences with a moved start; drop those the rule lost."""
    shift = local_time(candidate.start) - local_time(event.start)
    all_day = candidate.all_day

    def moved(key: str) -> str:
        return occurrence_key(local_time(key) + shift, all_day=all_day)

    exdates = [moved(key) for key in event.exdates]
    overrides = {moved(key): dict(value) for key, value in event.overrides.items()}
    produced = occurrence_keys(candidate, [*exdates, *overrides])
    candidate.exdates = sorted(key for key in exdates if key in produced)
    candidate.overrides = {key: value for key, value in overrides.items() if key in produced}


def _without_series_values(
    event: CalendarEvent, key: str, override: dict[str, Any]
) -> dict[str, Any]:
    """Drop the changes of one occurrence that equal what the series gives it."""
    result = dict(override)
    for name in ("title", "description", "location"):
        if name in result and result[name] == getattr(event, name):
            del result[name]
    if "start" in result:
        own_start = local_time(key)
        own_end = own_start + (local_time(event.end) - local_time(event.start))
        if (result["start"], result["end"]) == (
            occurrence_key(own_start, all_day=event.all_day),
            occurrence_key(own_end, all_day=event.all_day),
        ):
            del result["start"], result["end"]
    return result


def _changed_fields(before: CalendarEvent, after: CalendarEvent) -> list[str]:
    old, new = before.to_dict(), after.to_dict()
    return sorted(name for name in new if name != "updated_at" and new[name] != old[name])


def _check_year(year: int, field_name: str) -> None:
    if not FIRST_EVENT_YEAR <= year <= LAST_EVENT_YEAR:
        raise CalendarValidationError(
            f"{field_name} must be between the years {FIRST_EVENT_YEAR} and {LAST_EVENT_YEAR}"
        )


def _checked_window(window_start: datetime, window_end: datetime) -> tuple[datetime, datetime]:
    start, end = _as_utc(window_start), _as_utc(window_end)
    if end <= start:
        raise CalendarValidationError("window end must be after its start")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise CalendarValidationError(f"window span must not exceed {MAX_WINDOW_DAYS} days")
    return start, end
