"""Calendar event catalog, recurrence orchestration and mutation lifecycle."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from core.calendar._events import (
    _EVENT_INPUT_FIELDS,
    _EXDATE_FORMAT_ERROR,
    DEFAULT_ALL_DAY_DURATION_DAYS,
    DEFAULT_EVENT_DURATION_MINUTES,
    FIND_FREE_MAX_RESULTS,
    FIND_FREE_ROUNDING_MINUTES,
    MAX_CALENDAR_EVENTS,
    MAX_DURATION_DAYS,
    MAX_DURATION_MINUTES,
    MAX_EXDATES_PER_EVENT,
    MAX_NOTES_LENGTH,
    MAX_OCCURRENCES_PER_EVENT,
    MAX_TITLE_LENGTH,
    MAX_WINDOW_DAYS,
    CalendarEvent,
    EventOccurrence,
    FreeSlot,
    _clone_event,
    _event_to_inputs,
    _is_valid_duration_days,
    _is_valid_duration_minutes,
    _load_events_payload,
    _validate_duration_days,
    _validate_duration_minutes,
    _validate_event_data,
    _validate_required_text,
    _validate_text,
    validate_calendar_events_data,
    validate_calendar_events_file,
)
from core.calendar._time import (
    _DATE_ONLY_PATTERN_LENGTH,
    _as_utc,
    _default_timezone,
    _local_naive_iso,
    _looks_like_date,
    _merge_intervals,
    _parse_iso_datetime,
    _parse_utc_instant,
    _resolve_zone,
    _round_up_to_minutes,
    _utc_now,
    _utc_now_iso,
)
from core.calendar.actions import CalendarActions
from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarStorageError,
    CalendarValidationError,
)
from core.calendar.recurrence import (
    expand_recurring_allday,
    expand_recurring_timed,
    normalize_rrule,
    parse_date_string,
)
from core.calendar.when import parse_when
from core.config_validation import (
    JsonDiagnostic,
)
from core.utils.atomic import atomic_write_text
from core.utils.ids import new_id
from core.utils.logging import get_logger

_LOGGER = get_logger("calendar.service")

__all__ = [
    "MAX_CALENDAR_EVENTS",
    "MAX_EXDATES_PER_EVENT",
    "MAX_OCCURRENCES_PER_EVENT",
    "MAX_WINDOW_DAYS",
    "MAX_TITLE_LENGTH",
    "MAX_NOTES_LENGTH",
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
    """Manage persisted calendar events, expansion, and free-slot search."""

    def __init__(self, data_root: str | Path, *, tz: str | ZoneInfo | None = None) -> None:
        self._data_root = Path(data_root).expanduser()
        self._calendar_dir = self._data_root / "calendar"
        self._events_path = self._calendar_dir / "events.json"
        self._timezone = _resolve_zone(tz) if isinstance(tz, str) else (tz or _default_timezone())
        self._events: dict[str, CalendarEvent] = {}
        self._invalid_event_entries: list[Any] = []
        self._storage_load_error: CalendarStorageError | None = None
        self._events_loaded = False
        self._changed_callbacks: set[Callable[[], None]] = set()
        self.actions = CalendarActions(self, self._data_root)

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

    def create_event(
        self,
        *,
        title: str,
        start: str,
        all_day: bool | None = None,
        duration_minutes: int | None = None,
        duration_days: int | None = None,
        rrule: object | None = None,
        exdates: list[str] | None = None,
        notes: str | None = None,
    ) -> CalendarEvent:
        """Create and persist a new calendar event."""
        self._ensure_events_loaded()
        if len(self._events) >= MAX_CALENDAR_EVENTS:
            raise CalendarValidationError(
                f"The calendar stores at most {MAX_CALENDAR_EVENTS} events; delete old ones first"
            )
        event = self._build_event(
            created_at=_utc_now_iso(),
            title=title,
            all_day=all_day,
            start=start,
            duration_minutes=duration_minutes,
            duration_days=duration_days,
            rrule=rrule,
            exdates=exdates,
            notes=notes,
        )
        self._events[event.id] = event
        try:
            self._save_events()
        except Exception:
            self._events.pop(event.id, None)
            raise
        self._notify_changed()
        _LOGGER.info(
            "Calendar event created (event=%s recurring=%s)", event.id, event.rrule is not None
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
        event = self._events.get(event_id)
        if event is None:
            raise CalendarEventNotFoundError(f"Calendar event not found: {event_id}")
        return _clone_event(event)

    def update_event(self, event_id: str, **fields: Any) -> CalendarEvent:
        """Update one event from the same input shapes as create; omitted fields keep."""
        self._ensure_events_loaded()
        event = self._events.get(event_id)
        if event is None:
            raise CalendarEventNotFoundError(f"Calendar event not found: {event_id}")
        unknown_fields = sorted(set(fields) - _EVENT_INPUT_FIELDS)
        if unknown_fields:
            raise CalendarValidationError(
                f"Unsupported calendar event fields: {', '.join(unknown_fields)}"
            )

        inputs = _event_to_inputs(event)
        inputs.update(fields)
        # Clearing recurrence leaves any exdates meaningless (a single event can
        # hold no exceptions); drop them so a "no longer repeating" update does
        # not trip the single-event validation.
        if inputs.get("rrule") is None:
            inputs["exdates"] = []
        if inputs == _event_to_inputs(event):
            return _clone_event(event)

        candidate = self._build_event(
            created_at=event.created_at,
            **inputs,
        )
        candidate.id = event_id
        # A title/duration edit must retain the recurrence's original wall-clock zone.
        if candidate.rrule is not None and event.rrule is not None and "start" not in fields:
            candidate.tz_name = event.tz_name
        self._events[event_id] = candidate
        try:
            self._save_events()
        except Exception:
            self._events[event_id] = event
            raise
        self._notify_changed()
        _LOGGER.info("Calendar event updated (event=%s)", event_id)
        return _clone_event(candidate)

    def delete_event(self, event_id: str) -> None:
        """Delete one event by id."""
        self._ensure_events_loaded()
        if event_id not in self._events:
            raise CalendarEventNotFoundError(f"Calendar event not found: {event_id}")
        removed = self._events.pop(event_id)
        try:
            self._save_events()
        except Exception:
            self._events[event_id] = removed
            raise
        self._notify_changed()
        _LOGGER.info("Calendar event deleted (event=%s)", event_id)

    def add_exdate(self, event_id: str, occurrence_start: str) -> CalendarEvent:
        """Exclude one occurrence of a recurring event (RFC 5545 EXDATE)."""
        self._ensure_events_loaded()
        event = self._events.get(event_id)
        if event is None:
            raise CalendarEventNotFoundError(f"Calendar event not found: {event_id}")
        if event.rrule is None:
            raise CalendarValidationError(
                "Single events cannot exclude occurrences; delete the event instead"
            )
        normalized = self._validate_exdate_value(occurrence_start, event)
        if normalized in event.exdates:
            return _clone_event(event)
        updated = _clone_event(event)
        updated.exdates = [*event.exdates, normalized]
        updated.updated_at = _utc_now_iso()
        self._validate_event(updated)
        self._events[event_id] = updated
        try:
            self._save_events()
        except Exception:
            self._events[event_id] = event
            raise
        self._notify_changed()
        return _clone_event(updated)

    def occurrences_in_window(
        self,
        window_start_utc: datetime,
        window_end_utc: datetime,
        *,
        max_per_event: int = MAX_OCCURRENCES_PER_EVENT,
    ) -> list[EventOccurrence]:
        """Expand all events into occurrences overlapping the half-open window."""
        self._ensure_events_loaded(allow_degraded=True)
        window_start = _as_utc(window_start_utc)
        window_end = _as_utc(window_end_utc)
        if window_end <= window_start:
            raise CalendarValidationError("window end must be after its start")
        if (window_end - window_start).days > MAX_WINDOW_DAYS:
            raise CalendarValidationError(f"window span must not exceed {MAX_WINDOW_DAYS} days")
        system_tz = self._timezone
        occurrences: list[EventOccurrence] = []
        for event in self._events.values():
            occurrences.extend(
                self._event_occurrences(event, window_start, window_end, system_tz, max_per_event)
            )
        occurrences.sort(
            key=lambda item: (
                item.start_utc
                or datetime.combine(item.start_date or date.min, time.min, tzinfo=UTC),
                item.event_id,
            )
        )
        return occurrences

    def event_occurrences(
        self, event: CalendarEvent, window_start: datetime, window_end: datetime
    ) -> list[EventOccurrence]:
        """Expand one known event for action scheduling using canonical recurrence rules."""
        return self._event_occurrences(
            event, window_start, window_end, self._timezone, MAX_OCCURRENCES_PER_EVENT
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
        """Find the earliest free spans of the requested duration in the window.

        Timed events block their span; all-day events block their whole local
        days. Slots start no earlier than the current time and align to
        five-minute boundaries.
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
        window_start = _as_utc(window_start_utc)
        window_end = _as_utc(window_end_utc)
        if window_end <= window_start:
            raise CalendarValidationError("window end must be after its start")
        if (window_end - window_start).days > MAX_WINDOW_DAYS:
            raise CalendarValidationError(f"window span must not exceed {MAX_WINDOW_DAYS} days")
        reference_now = _as_utc(now_utc) if now_utc is not None else datetime.now(UTC)
        duration = timedelta(minutes=duration_minutes)

        self._ensure_events_loaded(allow_degraded=True)
        busy: list[tuple[datetime, datetime]] = []
        for event in self._events.values():
            if event.all_day:
                for start_date, end_date in self._allday_occurrence_dates(
                    event, window_start, window_end
                ):
                    busy_start = datetime.combine(start_date, time.min, tzinfo=self._timezone)
                    busy_end = datetime.combine(end_date, time.min, tzinfo=self._timezone)
                    busy.append(
                        (
                            max(busy_start.astimezone(UTC), window_start),
                            min(busy_end.astimezone(UTC), window_end),
                        )
                    )
            else:
                for start_utc, end_utc in self._timed_occurrence_spans(
                    event, window_start, window_end
                ):
                    busy.append((max(start_utc, window_start), min(end_utc, window_end)))

        merged = _merge_intervals(busy)
        cursor = max(window_start, reference_now)
        cursor = _round_up_to_minutes(cursor, FIND_FREE_ROUNDING_MINUTES)
        slots: list[FreeSlot] = []
        for busy_start, busy_end in merged:
            gap_end = min(busy_start, window_end)
            if cursor + duration <= gap_end and len(slots) < max_results:
                slots.append(FreeSlot(start_utc=cursor, end_utc=cursor + duration))
            cursor = max(cursor, busy_end)
            if cursor >= window_end or len(slots) >= max_results:
                break
        if len(slots) < max_results and cursor + duration <= window_end:
            slots.append(FreeSlot(start_utc=cursor, end_utc=cursor + duration))
        return slots

    def _event_occurrences(
        self,
        event: CalendarEvent,
        window_start: datetime,
        window_end: datetime,
        system_tz: ZoneInfo,
        max_per_event: int,
    ) -> list[EventOccurrence]:
        recurring = event.rrule is not None
        if event.all_day:
            pairs = self._allday_occurrence_dates(event, window_start, window_end)
            return [
                EventOccurrence(
                    event_id=event.id,
                    title=event.title,
                    notes=event.notes,
                    all_day=True,
                    recurring=recurring,
                    start_utc=None,
                    end_utc=None,
                    start_date=start_date,
                    end_date=end_date,
                    occurrence_start=start_date.isoformat(),
                    occurrence_end=None,
                )
                for start_date, end_date in pairs
            ]
        spans = self._timed_occurrence_spans(event, window_start, window_end)
        zone = _resolve_zone(event.tz_name) if event.tz_name else system_tz
        return [
            EventOccurrence(
                event_id=event.id,
                title=event.title,
                notes=event.notes,
                all_day=False,
                recurring=recurring,
                start_utc=start_utc,
                end_utc=end_utc,
                start_date=None,
                end_date=None,
                occurrence_start=_local_naive_iso(start_utc, zone),
                occurrence_end=_local_naive_iso(end_utc, zone),
            )
            for start_utc, end_utc in spans
        ]

    def _timed_occurrence_spans(
        self,
        event: CalendarEvent,
        window_start: datetime,
        window_end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        if event.rrule is None:
            start_utc = _parse_utc_instant(event.start_utc or "", field_name="start_utc")
            end_utc = start_utc + timedelta(minutes=event.duration_minutes or 0)
            if end_utc <= window_start or start_utc >= window_end:
                return []
            return [(start_utc, end_utc)]
        assert event.start_local is not None and event.tz_name is not None
        tz = _resolve_zone(event.tz_name)
        return expand_recurring_timed(
            start_local=datetime.fromisoformat(event.start_local),
            tz=tz,
            rrule_spec=event.rrule,
            duration_minutes=int(event.duration_minutes or DEFAULT_EVENT_DURATION_MINUTES),
            exdates=frozenset(event.exdates),
            window_start_utc=window_start,
            window_end_utc=window_end,
            max_occurrences=MAX_OCCURRENCES_PER_EVENT,
        )

    def _allday_occurrence_dates(
        self,
        event: CalendarEvent,
        window_start: datetime,
        window_end: datetime,
    ) -> list[tuple[date, date]]:
        duration_days = int(event.duration_days or DEFAULT_ALL_DAY_DURATION_DAYS)
        if event.rrule is None:
            start_date = parse_date_string(event.start_date, field_name="start_date")
            end_date = start_date + timedelta(days=duration_days)
            window_start_date = window_start.astimezone(self._timezone).date()
            window_end_date = window_end.astimezone(self._timezone).date()
            if end_date <= window_start_date or start_date >= window_end_date:
                return []
            return [(start_date, end_date)]
        assert event.start_date is not None
        return expand_recurring_allday(
            start_date=parse_date_string(event.start_date, field_name="start_date"),
            duration_days=int(event.duration_days or DEFAULT_ALL_DAY_DURATION_DAYS),
            rrule_spec=event.rrule,
            exdates=frozenset(event.exdates),
            window_start_utc=window_start,
            window_end_utc=window_end,
            system_tz=self._timezone,
            max_occurrences=MAX_OCCURRENCES_PER_EVENT,
        )

    def parse_window_bound(self, value: str, *, is_end: bool) -> datetime:
        """Parse one window bound: a date (local day) or an ISO 8601 datetime."""
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            raise CalendarValidationError("window bounds must be non-empty strings")
        if len(text) == _DATE_ONLY_PATTERN_LENGTH and _looks_like_date(text):
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
        if window_end <= window_start:
            raise CalendarValidationError("window end must be after its start")
        if (window_end - window_start).days > MAX_WINDOW_DAYS:
            raise CalendarValidationError(f"window span must not exceed {MAX_WINDOW_DAYS} days")
        return window_start, window_end

    def resolve_when(
        self, value: str, *, now_utc: datetime | None = None
    ) -> tuple[datetime, datetime]:
        """Resolve one ``when`` expression (see core.calendar.when) to a UTC window."""
        return parse_when(value, now_utc=now_utc or _utc_now(), tz=self._timezone)

    def _build_event(
        self,
        *,
        created_at: str,
        title: object,
        start: object,
        all_day: object = None,
        duration_minutes: object = None,
        duration_days: object = None,
        rrule: object = None,
        exdates: object = None,
        notes: object = None,
    ) -> CalendarEvent:
        validated_title = _validate_required_text(
            title, field_name="title", max_length=MAX_TITLE_LENGTH
        )
        validated_notes = _validate_text(notes, field_name="notes", max_length=MAX_NOTES_LENGTH)
        normalized_rrule = normalize_rrule(rrule)
        recurring = normalized_rrule is not None

        if not isinstance(start, str) or not start.strip():
            raise CalendarValidationError(
                "start must be a date (YYYY-MM-DD) or an ISO 8601 datetime"
            )
        start_text = start.strip()
        is_date_only = len(start_text) == _DATE_ONLY_PATTERN_LENGTH and _looks_like_date(start_text)

        if is_date_only:
            if all_day is False:
                raise CalendarValidationError(
                    "all_day is false but start is a date; pass a datetime for timed events"
                )
            return self._build_allday_event(
                created_at=created_at,
                title=validated_title,
                notes=validated_notes,
                start_date=start_text,
                duration_days=duration_days,
                normalized_rrule=normalized_rrule,
                exdates=exdates,
            )

        if all_day is True:
            raise CalendarValidationError(
                "all_day events use a YYYY-MM-DD date as start, not a datetime"
            )
        parsed_start = _parse_iso_datetime(start_text, field_name="start")
        return self._build_timed_event(
            created_at=created_at,
            title=validated_title,
            notes=validated_notes,
            parsed_start=parsed_start,
            recurring=recurring,
            normalized_rrule=normalized_rrule,
            duration_minutes=duration_minutes,
            exdates=exdates,
        )

    def _build_timed_event(
        self,
        *,
        created_at: str,
        title: str,
        notes: str | None,
        parsed_start: datetime,
        recurring: bool,
        normalized_rrule: dict[str, Any] | None,
        duration_minutes: object,
        exdates: object,
    ) -> CalendarEvent:
        validated_duration = _validate_duration_minutes(duration_minutes)
        if recurring:
            # Recurring timed events anchor wall-clock in the server timezone;
            # 09:00 stays 09:00 across DST transitions.
            zone = self._timezone
            if parsed_start.tzinfo is None:
                wall_start = parsed_start
            else:
                wall_start = parsed_start.astimezone(zone).replace(tzinfo=None)
            event = CalendarEvent(
                id=new_id("evt", claim=lambda candidate: candidate not in self._events),
                title=title,
                all_day=False,
                notes=notes,
                start_utc=None,
                start_local=wall_start.replace(microsecond=0).isoformat(),
                tz_name=str(self._timezone),
                start_date=None,
                duration_minutes=validated_duration,
                duration_days=None,
                rrule=normalized_rrule,
                exdates=[],
                created_at=created_at,
                updated_at=_utc_now_iso(),
            )
            event.exdates = self._validate_exdate_list(exdates, event)
            self._validate_event(event)
            return event

        if parsed_start.tzinfo is None:
            parsed_start = parsed_start.replace(tzinfo=self._timezone)
        if exdates:
            raise CalendarValidationError(
                "exdates are only valid on recurring events; delete the event instead"
            )
        event = CalendarEvent(
            id=new_id("evt", claim=lambda candidate: candidate not in self._events),
            title=title,
            all_day=False,
            notes=notes,
            start_utc=parsed_start.astimezone(UTC).isoformat(),
            start_local=None,
            tz_name=None,
            start_date=None,
            duration_minutes=validated_duration,
            duration_days=None,
            rrule=None,
            exdates=[],
            created_at=created_at,
            updated_at=_utc_now_iso(),
        )
        self._validate_event(event)
        return event

    def _build_allday_event(
        self,
        *,
        created_at: str,
        title: str,
        notes: str | None,
        start_date: str,
        duration_days: object,
        normalized_rrule: dict[str, Any] | None,
        exdates: object,
    ) -> CalendarEvent:
        validated_duration = _validate_duration_days(duration_days)
        event = CalendarEvent(
            id=new_id("evt", claim=lambda candidate: candidate not in self._events),
            title=title,
            all_day=True,
            notes=notes,
            start_utc=None,
            start_local=None,
            tz_name=None,
            start_date=start_date,
            duration_minutes=None,
            duration_days=validated_duration,
            rrule=normalized_rrule,
            exdates=[],
            created_at=created_at,
            updated_at=_utc_now_iso(),
        )
        event.exdates = self._validate_exdate_list(exdates, event)
        self._validate_event(event)
        return event

    def _validate_event(self, event: CalendarEvent) -> None:
        if not event.title.strip():
            raise CalendarValidationError("title must be a non-empty string")
        if event.all_day:
            if event.start_date is None:
                raise CalendarValidationError("all-day events require start_date")
            parse_date_string(event.start_date, field_name="start_date")
            if not _is_valid_duration_days(event.duration_days):
                raise CalendarValidationError(
                    f"duration_days must be an integer between 1 and {MAX_DURATION_DAYS}"
                )
            if (
                event.start_utc
                or event.start_local
                or event.tz_name
                or event.duration_minutes is not None
            ):
                raise CalendarValidationError("all-day events must not carry timed fields")
        elif event.rrule is not None:
            if event.start_local is None or event.tz_name is None:
                raise CalendarValidationError(
                    "recurring timed events require start_local and tz_name"
                )
            _resolve_zone(event.tz_name)
            datetime.fromisoformat(event.start_local)
            if not _is_valid_duration_minutes(event.duration_minutes):
                raise CalendarValidationError(
                    f"duration_minutes must be an integer between 1 and {MAX_DURATION_MINUTES}"
                )
            if (
                event.start_utc is not None
                or event.start_date is not None
                or event.duration_days is not None
            ):
                raise CalendarValidationError("timed events must not carry all-day fields")
        else:
            if event.start_utc is None:
                raise CalendarValidationError("single timed events require start_utc")
            _parse_utc_instant(event.start_utc, field_name="start_utc")
            if not _is_valid_duration_minutes(event.duration_minutes):
                raise CalendarValidationError(
                    f"duration_minutes must be an integer between 1 and {MAX_DURATION_MINUTES}"
                )
            if (
                event.start_local is not None
                or event.tz_name is not None
                or event.start_date is not None
                or event.duration_days is not None
            ):
                raise CalendarValidationError("timed events must not carry all-day fields")

        if event.rrule is None and event.exdates:
            raise CalendarValidationError("exdates are only valid on recurring events")
        for exdate in event.exdates:
            self._validate_exdate_value(exdate, event)
        if len(event.exdates) > MAX_EXDATES_PER_EVENT:
            raise CalendarValidationError(
                f"events allow at most {MAX_EXDATES_PER_EVENT} excluded occurrences"
            )

    def _validate_exdate_value(self, value: object, event: CalendarEvent) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CalendarValidationError("exdate must be a non-empty string")
        text = value.strip()
        if event.all_day:
            return parse_date_string(text, field_name="exdate").isoformat()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise CalendarValidationError(_EXDATE_FORMAT_ERROR) from error
        if parsed.tzinfo is not None:
            raise CalendarValidationError(_EXDATE_FORMAT_ERROR)
        return parsed.replace(microsecond=0).isoformat()

    def _validate_exdate_list(self, exdates: object, event: CalendarEvent) -> list[str]:
        if exdates is None:
            return []
        if not isinstance(exdates, list):
            raise CalendarValidationError("exdates must be a list of occurrence start strings")
        if len(exdates) > MAX_EXDATES_PER_EVENT:
            raise CalendarValidationError(
                f"events allow at most {MAX_EXDATES_PER_EVENT} excluded occurrences"
            )
        normalized: list[str] = []
        for value in exdates:
            normalized_value = self._validate_exdate_value(value, event)
            if normalized_value not in normalized:
                normalized.append(normalized_value)
        return normalized

    def _load_events(self) -> dict[str, CalendarEvent]:
        self._ensure_storage_exists()
        raw_payload = _load_events_payload(self._events_path)
        self._invalid_event_entries = []
        events: dict[str, CalendarEvent] = {}
        for index, item in enumerate(raw_payload):
            diagnostics: list[JsonDiagnostic] = []
            _validate_event_data(diagnostics, index, item)
            errors = [diagnostic for diagnostic in diagnostics if diagnostic.severity == "error"]
            if errors:
                details = "; ".join(
                    f"{diagnostic.path}: {diagnostic.message}" for diagnostic in errors
                )
                _LOGGER.warning("Skipping invalid calendar event: %s", details)
                self._invalid_event_entries.append(item)
                continue
            try:
                event = CalendarEvent.from_dict(cast("dict[str, Any]", item))
                self._validate_event(event)
            except (CalendarValidationError, TypeError, ValueError) as error:
                _LOGGER.warning("Skipping invalid calendar event at $[%d]: %s", index, error)
                self._invalid_event_entries.append(item)
                continue
            if event.id in events:
                _LOGGER.warning(
                    "Skipping duplicate calendar event id at $[%d]: %s", index, event.id
                )
                self._invalid_event_entries.append(item)
                continue
            events[event.id] = event
        return events

    def _save_events(self) -> None:
        self._ensure_storage_exists()
        payload = [
            event.to_dict()
            for event in sorted(self._events.values(), key=lambda item: (item.created_at, item.id))
        ] + list(self._invalid_event_entries)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            atomic_write_text(self._events_path, serialized)
        except OSError as error:
            raise CalendarStorageError(f"Cannot write {self._events_path}: {error}") from error

    def _notify_changed(self) -> None:
        self._notify_callbacks()

    def _notify_action_changed(self) -> None:
        """Publish execution progress without withdrawing action admission."""
        self._notify_callbacks(exclude=self.actions._wake)

    def _notify_callbacks(self, *, exclude: Callable[[], None] | None = None) -> None:
        for callback in tuple(self._changed_callbacks):
            if callback == exclude:
                continue
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
            if not self._events_path.exists():
                self._events_path.write_text("[]\n", encoding="utf-8")
        except OSError as error:
            raise CalendarStorageError(
                f"Cannot initialize calendar storage at {self._calendar_dir}: {error}"
            ) from error
