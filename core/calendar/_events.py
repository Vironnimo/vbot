"""Calendar event records, input validation and persisted JSON decoding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from core.calendar._time import (
    _utc_now_iso,
)
from core.calendar.errors import (
    CalendarStorageError,
    CalendarValidationError,
)
from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
    validate_non_empty_string,
    warn_unknown_keys,
)

MAX_CALENDAR_EVENTS = 2000


MAX_EXDATES_PER_EVENT = 1000


MAX_OCCURRENCES_PER_EVENT = 500


MAX_WINDOW_DAYS = 62


MAX_TITLE_LENGTH = 200


MAX_NOTES_LENGTH = 5000


MAX_DURATION_MINUTES = 60 * 24 * 30


MAX_DURATION_DAYS = 365


DEFAULT_EVENT_DURATION_MINUTES = 60


DEFAULT_ALL_DAY_DURATION_DAYS = 1


FIND_FREE_MAX_RESULTS = 5


FIND_FREE_ROUNDING_MINUTES = 5


_EXDATE_FORMAT_ERROR = (
    "exdate must be a naive local datetime in the event's timezone (YYYY-MM-DDTHH:MM:SS)"
)


_EVENT_FIELDS = frozenset(
    (
        "id",
        "title",
        "notes",
        "all_day",
        "start_utc",
        "start_local",
        "tz_name",
        "start_date",
        "duration_minutes",
        "duration_days",
        "rrule",
        "exdates",
        "created_at",
        "updated_at",
    )
)


_EVENT_INPUT_FIELDS = frozenset(
    (
        "title",
        "notes",
        "all_day",
        "start",
        "duration_minutes",
        "duration_days",
        "rrule",
        "exdates",
    )
)


@dataclass(frozen=True, slots=True)
class EventOccurrence:
    """One expanded occurrence of a calendar event inside a query window.

    ``occurrence_start`` is the occurrence's start in the event's own anchor
    form - a naive local datetime for timed events, a plain date for all-day
    events - exactly the value an EXDATE (single-occurrence removal) expects.
    """

    event_id: str
    title: str
    notes: str | None
    all_day: bool
    recurring: bool
    start_utc: datetime | None
    end_utc: datetime | None
    start_date: date | None
    end_date: date | None
    occurrence_start: str
    occurrence_end: str | None


@dataclass(frozen=True, slots=True)
class FreeSlot:
    """One free time span found by ``find_free_slots``."""

    start_utc: datetime
    end_utc: datetime


@dataclass(slots=True)
class CalendarEvent:
    """Persisted calendar event record.

    Exactly one start shape is set, enforced by validation: ``start_utc`` for
    single timed events (absolute instant), ``start_local`` + ``tz_name`` for
    recurring timed events (RFC 5545 wall-clock anchor), ``start_date`` for
    all-day events. Exceptions (``exdates``) exist only on recurring events and
    use the event's own start form.
    """

    id: str
    title: str
    all_day: bool
    notes: str | None
    start_utc: str | None
    start_local: str | None
    tz_name: str | None
    start_date: str | None
    duration_minutes: int | None
    duration_days: int | None
    rrule: dict[str, Any] | None
    exdates: list[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize one CalendarEvent to a JSON-compatible payload."""
        return {
            "id": self.id,
            "title": self.title,
            "notes": self.notes,
            "all_day": self.all_day,
            "start_utc": self.start_utc,
            "start_local": self.start_local,
            "tz_name": self.tz_name,
            "start_date": self.start_date,
            "duration_minutes": self.duration_minutes,
            "duration_days": self.duration_days,
            "rrule": self.rrule,
            "exdates": list(self.exdates),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CalendarEvent:
        """Create one CalendarEvent from persisted JSON data."""
        return cls(
            id=str(payload["id"]),
            title=str(payload["title"]),
            all_day=bool(payload.get("all_day") or False),
            notes=payload.get("notes"),
            start_utc=payload.get("start_utc"),
            start_local=payload.get("start_local"),
            tz_name=payload.get("tz_name"),
            start_date=payload.get("start_date"),
            duration_minutes=payload.get("duration_minutes"),
            duration_days=payload.get("duration_days"),
            rrule=payload.get("rrule"),
            exdates=[str(value) for value in payload.get("exdates") or []],
            created_at=str(payload.get("created_at") or _utc_now_iso()),
            updated_at=str(
                payload.get("updated_at") or payload.get("created_at") or _utc_now_iso()
            ),
        )


def validate_calendar_events_file(events_path: str | Path) -> JsonValidationReport:
    """Validate persisted ``calendar/events.json`` without consuming it."""
    return validate_json_file(events_path, validate_calendar_events_data, missing_ok=True)


def validate_calendar_events_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``calendar/events.json`` array."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, list):
        return [error_diagnostic("$", f"Expected a JSON array, got {type(data).__name__}")]
    for index, item in enumerate(data):
        _validate_event_data(diagnostics, index, item)
    return diagnostics


def _validate_event_data(diagnostics: list[JsonDiagnostic], index: int, item: Any) -> None:
    item_path = f"$[{index}]"
    if not isinstance(item, dict):
        add_error(diagnostics, item_path, "Expected a JSON object")
        return
    warn_unknown_keys(diagnostics, item_path, item, _EVENT_FIELDS, "calendar event field")
    validate_non_empty_string(diagnostics, f"{item_path}.id", item.get("id"), required=True)
    validate_non_empty_string(diagnostics, f"{item_path}.title", item.get("title"), required=True)
    for field_name in ("notes", "start_utc", "start_local", "tz_name", "start_date"):
        value = item.get(field_name)
        if value is not None and not isinstance(value, str):
            add_error(diagnostics, f"{item_path}.{field_name}", "must be a string when provided")
    for field_name in ("duration_minutes", "duration_days"):
        value = item.get(field_name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            add_error(diagnostics, f"{item_path}.{field_name}", "must be an integer when provided")
    if item.get("all_day") is not None and not isinstance(item.get("all_day"), bool):
        add_error(diagnostics, f"{item_path}.all_day", "must be a boolean when provided")
    rrule = item.get("rrule")
    if rrule is not None and not isinstance(rrule, dict):
        add_error(diagnostics, f"{item_path}.rrule", "must be an object when provided")
    exdates = item.get("exdates")
    if exdates is not None and (
        not isinstance(exdates, list) or not all(isinstance(value, str) for value in exdates)
    ):
        add_error(diagnostics, f"{item_path}.exdates", "must be a list of strings")
    validate_non_empty_string(
        diagnostics, f"{item_path}.created_at", item.get("created_at"), required=False
    )


def _event_to_inputs(event: CalendarEvent) -> dict[str, Any]:
    """Project one event back into the create/update input shape."""
    inputs: dict[str, Any] = {
        "title": event.title,
        "notes": event.notes,
        "all_day": event.all_day,
        "duration_minutes": event.duration_minutes,
        "duration_days": event.duration_days,
        "rrule": dict(event.rrule) if event.rrule is not None else None,
        "exdates": list(event.exdates),
        "start": (
            event.start_utc
            if event.start_utc is not None
            else event.start_local
            if event.start_local is not None
            else event.start_date
        ),
    }
    return inputs


def _clone_event(event: CalendarEvent) -> CalendarEvent:
    return CalendarEvent.from_dict(event.to_dict())


def _validate_text(
    value: object, *, field_name: str, max_length: int, required: bool = False
) -> str | None:
    if value is None:
        if required:
            raise CalendarValidationError(f"{field_name} must be a non-empty string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise CalendarValidationError(f"{field_name} must be a non-empty string when provided")
    text = value.strip()
    if len(text) > max_length:
        raise CalendarValidationError(f"{field_name} must not exceed {max_length} characters")
    return text


def _validate_required_text(value: object, *, field_name: str, max_length: int) -> str:
    validated = _validate_text(value, field_name=field_name, max_length=max_length, required=True)
    assert validated is not None
    return validated


def _validate_duration_minutes(value: object) -> int:
    if value is None:
        return DEFAULT_EVENT_DURATION_MINUTES
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not _is_valid_duration_minutes(value)
    ):
        raise CalendarValidationError(
            f"duration_minutes must be an integer between 1 and {MAX_DURATION_MINUTES}"
        )
    return value


def _is_valid_duration_minutes(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 1 <= value <= MAX_DURATION_MINUTES
    )


def _validate_duration_days(value: object) -> int:
    if value is None:
        return DEFAULT_ALL_DAY_DURATION_DAYS
    if isinstance(value, bool) or not isinstance(value, int) or not _is_valid_duration_days(value):
        raise CalendarValidationError(
            f"duration_days must be an integer between 1 and {MAX_DURATION_DAYS}"
        )
    return value


def _is_valid_duration_days(value: object) -> bool:
    return (
        not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= MAX_DURATION_DAYS
    )


def _load_events_payload(events_path: str | Path) -> list[Any]:
    """Load the JSON array without letting one bad event reject its siblings."""
    try:
        return cast(
            "list[Any]",
            load_validated_json_file(
                events_path,
                _validate_events_container,
                missing_ok=True,
                missing_default=[],
            ),
        )
    except JsonConfigValidationError as error:
        raise CalendarStorageError(str(error)) from error


def _validate_events_container(data: Any) -> list[JsonDiagnostic]:
    if isinstance(data, list):
        return []
    return [error_diagnostic("$", f"Expected a JSON array, got {type(data).__name__}")]
