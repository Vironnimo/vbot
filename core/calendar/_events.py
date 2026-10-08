"""Calendar event records, occurrence values and the persisted ``calendar/events.json`` format.

A timed event keeps its start and end as naive local wall-clock times in its
own IANA zone (``tz_name``), the server zone when it was created. An all-day
event keeps dates, with an exclusive end, and has no zone: its days start at
local midnight in the server zone. A repeating event keeps one RFC 5545 RRULE;
its occurrences are named by their original start in the event's own form,
which removed occurrences (``exdates``) and changed ones (``overrides``) use
as their key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from core.calendar._time import _resolve_zone, _utc_now_iso
from core.calendar.errors import CalendarStorageError, CalendarValidationError
from core.calendar.recurrence import normalize_rrule
from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    load_validated_json_file,
    validate_json_file,
    validate_non_empty_string,
)
from core.json_documents import (
    FORMAT_VERSION_FIELD,
    JsonDocumentFormat,
    json_document,
    json_list,
    json_map,
    json_object,
    strip_unknown_fields,
    validate_collection_root,
    warn_unknown_fields,
)

MAX_CALENDAR_EVENTS = 2000
MAX_EXDATES_PER_EVENT = 1000
MAX_OVERRIDES_PER_EVENT = 1000
MAX_OCCURRENCES_PER_EVENT = 500
MAX_WINDOW_DAYS = 62
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 5000
MAX_LOCATION_LENGTH = 500
MAX_DURATION_MINUTES = 60 * 24 * 30
MAX_DURATION_DAYS = 365
DEFAULT_EVENT_DURATION_MINUTES = 60
DEFAULT_ALL_DAY_DURATION_DAYS = 1
FIND_FREE_MAX_RESULTS = 10
FIND_FREE_ROUNDING_MINUTES = 5

CALENDAR_EVENTS_FORMAT_VERSION = 2
# The format of earlier vBot versions, whose events this version ignores.
LEGACY_CALENDAR_EVENTS_FORMAT_VERSION = 1

OVERRIDE_FIELDS = frozenset(("title", "description", "location", "start", "end"))
_EVENT_FIELDS = frozenset(
    (
        "id",
        "title",
        "description",
        "location",
        "start",
        "end",
        "tz_name",
        "rrule",
        "exdates",
        "overrides",
        "created_at",
        "updated_at",
    )
)
CALENDAR_EVENT_SHAPE = json_object(
    _EVENT_FIELDS, {"overrides": json_map(json_object(OVERRIDE_FIELDS))}
)
CALENDAR_EVENTS_SHAPE = json_document(
    {"events"}, {"events": json_list(CALENDAR_EVENT_SHAPE, key="id")}
)

# ``<event id>_YYYYMMDD`` for an all-day occurrence, ``<event id>_YYYYMMDDTHHMM[SS]``
# for a timed one: the occurrence's original start.
_OCCURRENCE_SUFFIX = re.compile(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?)?")


@dataclass(frozen=True, slots=True)
class EventOccurrence:
    """One occurrence of a calendar event, with its changes applied.

    ``start`` and ``end`` are in the event's own form: naive local times in its
    zone, or dates (exclusive end) for an all-day event. ``start_utc`` and
    ``end_utc`` are the instants; an all-day occurrence spans local midnights
    in the server zone. ``original_start`` is the key of an occurrence of a
    repeating event (None for a single event), ``id`` the occurrence id, which
    is the event id for a single event.
    """

    id: str
    event_id: str
    title: str
    description: str | None
    location: str | None
    all_day: bool
    recurring: bool
    start: str
    end: str
    start_utc: datetime
    end_utc: datetime
    original_start: str | None
    overridden: bool


@dataclass(frozen=True, slots=True)
class FreeSlot:
    """One free time span found by ``find_free_slots``."""

    start_utc: datetime
    end_utc: datetime


@dataclass(slots=True)
class CalendarEvent:
    """Persisted calendar event: a single event or a repeating series.

    ``overrides`` maps an occurrence key to the fields that occurrence changes:
    title, description, location, and start with end. A missing field keeps the
    series' value; a null description or location clears it.
    """

    id: str
    title: str
    description: str | None
    location: str | None
    start: str
    end: str
    tz_name: str | None
    rrule: str | None
    exdates: list[str] = field(default_factory=list)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def all_day(self) -> bool:
        """Whether the event spans whole days."""
        return self.tz_name is None

    @property
    def recurring(self) -> bool:
        """Whether the event repeats."""
        return self.rrule is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialize one CalendarEvent to its persisted JSON form."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "location": self.location,
            "start": self.start,
            "end": self.end,
            "tz_name": self.tz_name,
            "rrule": self.rrule,
            "exdates": list(self.exdates),
            "overrides": {key: dict(value) for key, value in self.overrides.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CalendarEvent:
        """Create one CalendarEvent from persisted JSON data."""
        created_at = str(payload.get("created_at") or _utc_now_iso())
        return cls(
            id=str(payload["id"]),
            title=str(payload["title"]),
            description=payload.get("description"),
            location=payload.get("location"),
            start=str(payload["start"]),
            end=str(payload["end"]),
            tz_name=payload.get("tz_name"),
            rrule=payload.get("rrule"),
            exdates=[str(value) for value in payload.get("exdates") or []],
            overrides={
                str(key): dict(value) for key, value in (payload.get("overrides") or {}).items()
            },
            created_at=created_at,
            updated_at=str(payload.get("updated_at") or created_at),
        )


def occurrence_id(event_id: str, key: str) -> str:
    """The id of the occurrence of ``event_id`` that originally starts at ``key``."""
    digits = key.replace("-", "").replace(":", "")
    if len(digits) == 15 and digits.endswith("00"):
        digits = digits[:-2]
    return f"{event_id}_{digits}"


def parse_occurrence_id(value: str) -> tuple[str, str] | None:
    """Split an occurrence id into its event id and occurrence key; None for any other id."""
    event_id, separator, suffix = value.rpartition("_")
    match = _OCCURRENCE_SUFFIX.fullmatch(suffix) if separator else None
    if match is None or "_" not in event_id:
        return None
    year, month, day, hour, minute, second = match.groups()
    key = f"{year}-{month}-{day}"
    if hour is not None:
        key += f"T{hour}:{minute}:{second or '00'}"
    try:
        datetime.fromisoformat(key)
    except ValueError:
        return None
    return event_id, key


def validate_calendar_events_file(events_path: str | Path) -> JsonValidationReport:
    """Validate persisted ``calendar/events.json`` without consuming it."""
    return validate_json_file(events_path, validate_calendar_events_data, missing_ok=True)


def validate_calendar_events_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``calendar/events.json`` document."""
    diagnostics: list[JsonDiagnostic] = []
    entries = _validate_events_root(diagnostics, data)
    for index, item in enumerate(entries or []):
        _read_event(diagnostics, f"$.events[{index}]", item)
    return diagnostics


def _read_event(
    diagnostics: list[JsonDiagnostic], item_path: str, item: Any
) -> CalendarEvent | None:
    """Check one stored event and return it, or None after reporting why it is invalid."""
    known = len(diagnostics)
    _validate_event_data(diagnostics, item_path, item)
    if any(diagnostic.severity == "error" for diagnostic in diagnostics[known:]):
        return None
    try:
        event = CalendarEvent.from_dict(
            cast("dict[str, Any]", strip_unknown_fields(item, CALENDAR_EVENT_SHAPE))
        )
        _check_event(event)
    except (CalendarValidationError, TypeError, ValueError) as error:
        add_error(diagnostics, item_path, str(error))
        return None
    return event


def is_legacy_events_document(data: Any) -> bool:
    """Whether a decoded document holds the events of an earlier vBot version."""
    if not isinstance(data, dict):
        return False
    version = data.get(FORMAT_VERSION_FIELD)
    return not isinstance(version, bool) and version == LEGACY_CALENDAR_EVENTS_FORMAT_VERSION


def _validate_events_root(diagnostics: list[JsonDiagnostic], data: Any) -> list[Any] | None:
    if is_legacy_events_document(data):
        diagnostics.append(
            JsonDiagnostic(
                "warning",
                f"$.{FORMAT_VERSION_FIELD}",
                f"is {LEGACY_CALENDAR_EVENTS_FORMAT_VERSION}: the calendar events of an earlier "
                "vBot version, which this vBot ignores; the next change of the calendar "
                "replaces them",
            )
        )
        return None
    return validate_collection_root(
        diagnostics,
        data,
        version=CALENDAR_EVENTS_FORMAT_VERSION,
        shape=CALENDAR_EVENTS_SHAPE,
        collection="events",
        label="calendar events field",
    )


def _events_root_diagnostics(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    _validate_events_root(diagnostics, data)
    return diagnostics


# The event file keeps invalid entries verbatim, so only an unreadable document
# root refuses a write.
CALENDAR_EVENTS_FORMAT = JsonDocumentFormat(
    name="Calendar events",
    version=CALENDAR_EVENTS_FORMAT_VERSION,
    shape=CALENDAR_EVENTS_SHAPE,
    validate=_events_root_diagnostics,
    sort_keys=True,
)


def _validate_event_data(diagnostics: list[JsonDiagnostic], item_path: str, item: Any) -> None:
    """Check the JSON types of one stored event; the service checks what they mean."""
    if not isinstance(item, dict):
        add_error(diagnostics, item_path, "Expected a JSON object")
        return
    warn_unknown_fields(
        diagnostics, item_path, item, CALENDAR_EVENT_SHAPE, label="calendar event field"
    )
    for field_name in ("id", "title", "start", "end"):
        validate_non_empty_string(
            diagnostics, f"{item_path}.{field_name}", item.get(field_name), required=True
        )
    for field_name in ("description", "location", "tz_name", "rrule"):
        _validate_optional_string(diagnostics, f"{item_path}.{field_name}", item.get(field_name))
    exdates = item.get("exdates")
    if exdates is not None and (
        not isinstance(exdates, list) or not all(isinstance(value, str) for value in exdates)
    ):
        add_error(diagnostics, f"{item_path}.exdates", "must be a list of strings")
    overrides = item.get("overrides")
    if overrides is not None and not isinstance(overrides, dict):
        add_error(diagnostics, f"{item_path}.overrides", "must be an object")
    for key, override in (overrides or {}).items() if isinstance(overrides, dict) else ():
        override_path = f"{item_path}.overrides.{key}"
        if not isinstance(override, dict):
            add_error(diagnostics, override_path, "must be an object")
            continue
        for field_name in OVERRIDE_FIELDS:
            _validate_optional_string(
                diagnostics, f"{override_path}.{field_name}", override.get(field_name)
            )
    validate_non_empty_string(
        diagnostics, f"{item_path}.created_at", item.get("created_at"), required=False
    )


def _validate_optional_string(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        add_error(diagnostics, path, "must be a string when provided")


def _clone_event(event: CalendarEvent) -> CalendarEvent:
    return CalendarEvent.from_dict(event.to_dict())


def _load_events_document(events_path: str | Path) -> dict[str, Any] | None:
    """Load the events document without letting one bad event reject its siblings."""
    try:
        data = load_validated_json_file(events_path, _events_root_diagnostics, missing_ok=True)
    except JsonConfigValidationError as error:
        raise CalendarStorageError(str(error)) from error
    return None if data is None else cast("dict[str, Any]", data)


def _is_date_text(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return len(value) == 10


def _check_event(event: CalendarEvent) -> None:
    """Refuse an event record whose fields contradict each other or the limits."""
    _required_text(event.title, "title", MAX_TITLE_LENGTH)
    _optional_text(event.description, "description", MAX_DESCRIPTION_LENGTH)
    _optional_text(event.location, "location", MAX_LOCATION_LENGTH)
    all_day = event.tz_name is None
    zone = None if event.tz_name is None else _resolve_zone(event.tz_name)
    start = _stored_time(event.start, "start", all_day=all_day)
    end = _stored_time(event.end, "end", all_day=all_day)
    _check_span(start, end, all_day=all_day)
    if all_day and end == start:
        raise CalendarValidationError("end must be after start")
    if event.rrule is None:
        if event.exdates or event.overrides:
            raise CalendarValidationError(
                "only a repeating event has removed or changed occurrences"
            )
        return
    if normalize_rrule(event.rrule, start=start, zone=zone) is None:
        raise CalendarValidationError("rrule must not be empty")
    if len(event.exdates) > MAX_EXDATES_PER_EVENT:
        raise CalendarValidationError(
            f"events allow at most {MAX_EXDATES_PER_EVENT} removed occurrences"
        )
    if len(event.overrides) > MAX_OVERRIDES_PER_EVENT:
        raise CalendarValidationError(
            f"events allow at most {MAX_OVERRIDES_PER_EVENT} changed occurrences"
        )
    for key in event.exdates:
        _stored_time(key, "exdate", all_day=all_day)
    for key, override in event.overrides.items():
        _stored_time(key, "changed occurrence", all_day=all_day)
        _check_override(override, all_day=all_day)


def _check_override(override: dict[str, Any], *, all_day: bool) -> None:
    unknown = sorted(set(override) - OVERRIDE_FIELDS)
    if unknown:
        raise CalendarValidationError(f"unknown occurrence fields: {', '.join(unknown)}")
    if "title" in override:
        _required_text(override["title"], "title", MAX_TITLE_LENGTH)
    for name, limit in (("description", MAX_DESCRIPTION_LENGTH), ("location", MAX_LOCATION_LENGTH)):
        if name in override:
            _optional_text(override[name], name, limit)
    if ("start" in override) != ("end" in override):
        raise CalendarValidationError("a changed occurrence gives start and end together")
    if "start" in override:
        start = _stored_time(override["start"], "start", all_day=all_day)
        end = _stored_time(override["end"], "end", all_day=all_day)
        _check_span(start, end, all_day=all_day)
        if all_day and end == start:
            raise CalendarValidationError("end must be after start")


def _stored_time(value: object, field_name: str, *, all_day: bool) -> datetime:
    """A stored start, end or key in the event's form, as a naive datetime."""
    if not isinstance(value, str):
        raise CalendarValidationError(f"{field_name} must be a string")
    if all_day != _is_date_text(value):
        form = "a date (YYYY-MM-DD)" if all_day else "a local date-time (YYYY-MM-DDTHH:MM:SS)"
        raise CalendarValidationError(f"{field_name} must be {form}")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise CalendarValidationError(f"{field_name} must be a valid date or time") from error
    if moment.tzinfo is not None:
        raise CalendarValidationError(f"{field_name} must be a local time without an offset")
    return moment


def _check_span(start: datetime, end: datetime, *, all_day: bool) -> None:
    if all_day:
        if end < start:
            raise CalendarValidationError("end must not be before start")
        if end - start > timedelta(days=MAX_DURATION_DAYS):
            raise CalendarValidationError(
                f"an all-day event lasts at most {MAX_DURATION_DAYS} days"
            )
        return
    if end <= start:
        raise CalendarValidationError("end must be after start")
    if end - start > timedelta(minutes=MAX_DURATION_MINUTES):
        raise CalendarValidationError(
            f"a timed event lasts at most {MAX_DURATION_MINUTES // 1440} days"
        )


def _required_text(value: object, field_name: str, max_length: int) -> str:
    text = _optional_text(value, field_name, max_length)
    if text is None:
        raise CalendarValidationError(f"{field_name} must be a non-empty string")
    return text


def _optional_text(value: object, field_name: str, max_length: int) -> str | None:
    """A trimmed text; None or an empty text clears it."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise CalendarValidationError(f"{field_name} must be a string")
    text = value.strip()
    if len(text) > max_length:
        raise CalendarValidationError(f"{field_name} must not exceed {max_length} characters")
    return text or None
