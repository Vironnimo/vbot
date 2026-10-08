"""Calendar domain public API."""

from core.calendar._events import occurrence_id, parse_occurrence_id
from core.calendar.errors import (
    CalendarEventNotFoundError,
    CalendarServiceError,
    CalendarStorageError,
    CalendarValidationError,
)
from core.calendar.service import (
    MAX_CALENDAR_EVENTS,
    MAX_WINDOW_DAYS,
    CalendarEvent,
    CalendarService,
    EventOccurrence,
    FreeSlot,
    validate_calendar_events_data,
    validate_calendar_events_file,
)
from core.calendar.when import WHEN_GRAMMAR, parse_when

__all__ = [
    "MAX_CALENDAR_EVENTS",
    "MAX_WINDOW_DAYS",
    "WHEN_GRAMMAR",
    "CalendarEvent",
    "CalendarEventNotFoundError",
    "CalendarService",
    "CalendarServiceError",
    "CalendarStorageError",
    "CalendarValidationError",
    "EventOccurrence",
    "FreeSlot",
    "occurrence_id",
    "parse_occurrence_id",
    "parse_when",
    "validate_calendar_events_data",
    "validate_calendar_events_file",
]
