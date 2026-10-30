"""Calendar Tool reads and results: exact identities, complete pages and explicit time zones."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from heapq import merge
from itertools import islice, takewhile
from typing import Any
from zoneinfo import ZoneInfo

from core.calendar import (
    CalendarEvent,
    CalendarProjection,
    EventOccurrence,
    occurrence_id,
    parse_occurrence_id,
    parse_when,
)
from core.calendar.recurrence import resolve_local_span
from core.providers.adapter import tool_result_text
from core.tools._calendar_arguments import (
    STAND_INS,
    WHEN_FIELD,
    CalendarCallRefusedError,
    length_text,
    refusal,
)
from core.tools._calendar_pages import CalendarPage, Position, Record, catalog_identity
from core.tools.tools import JsonObject, tool_success


class _CalendarRead:
    def __init__(self, calendar: CalendarProjection, jobs: dict[str, str], now: datetime) -> None:
        self._calendar = calendar
        self._jobs = jobs
        self._now = now

    def _list(self, arguments: JsonObject) -> JsonObject:
        page, start, end = self._page(arguments, days=30)
        assert start is not None and end is not None
        arguments = page.arguments
        query = arguments.get("query", "").casefold()
        events = self._calendar.list_events()
        chosen_id = arguments.get("id")
        if chosen_id:
            parsed = parse_occurrence_id(chosen_id)
            events = [self._calendar.get_event(parsed[0] if parsed else chosen_id)]
        streams = [self._matching(event, start, end, query, page.position) for event in events]
        by_id = {event.id: event for event in events}

        # Prefixes are part of their record, so page bodies join losslessly.
        def content() -> Iterator[Record]:
            for item in merge(*streams, key=lambda item: (item.start_utc, item.event_id, item.id)):
                if chosen_id and parse_occurrence_id(chosen_id) and item.id != chosen_id:
                    continue
                position = (item.start_utc.isoformat(), item.event_id, item.id)
                yield (
                    position,
                    "\n\n" + _fields_text(self._occurrence_fields(item, by_id[item.event_id])),
                )

        data = {
            "occurrences": 0,
            "window": _window_text(start, end, self._zone()),
            "window_timezone": self._calendar.system_timezone_name(),
        }
        return tool_success(page.read(content(), data, visible_length=_visible_length))

    def _matching(
        self, event: CalendarEvent, start: datetime, end: datetime, query: str, position: Position
    ) -> Iterator[EventOccurrence]:
        if (
            query
            and not _matches(event, query)
            and not any(_matches(changes, query) for changes in event.overrides.values())
        ):
            return
        lower = max(start, datetime.fromisoformat(position[0])) if position else start
        for item in takewhile(
            lambda item: item.start_utc < end, self._calendar.iter_occurrences(event, lower)
        ):
            key = (item.start_utc.isoformat(), item.event_id, item.id)
            if position and key < position:
                continue
            if _matches(item, query):
                yield item

    def _get(self, arguments: JsonObject) -> JsonObject:
        page, _, _ = self._page(arguments)
        item_id = page.arguments.get("id")
        if not item_id:
            raise CalendarCallRefusedError(
                refusal("get needs id from list or search.", page.arguments, id=STAND_INS["id"])
            )
        parsed = parse_occurrence_id(item_id)
        if parsed is not None:
            event = self._calendar.get_event(parsed[0])
            fields = self._occurrence_fields(self._calendar.get_occurrence(item_id), event)
        else:
            event = self._calendar.get_event(item_id)
            fields = self._event_result(event)
            if event.exdates:
                fields["removed_occurrences"] = ", ".join(
                    occurrence_id(event.id, key) for key in sorted(event.exdates)
                )
            if event.overrides:
                fields["changed_occurrences"] = ", ".join(
                    occurrence_id(event.id, key) for key in sorted(event.overrides)
                )
        return tool_success(
            page.read(
                iter([((item_id,), _fields_text(fields))]), {}, visible_length=_visible_length
            )
        )

    def _search(self, arguments: JsonObject) -> JsonObject:
        page, _, _ = self._page(arguments)
        query = page.arguments.get("query", "").casefold()

        def records() -> Iterator[Record]:
            for event in self._calendar.list_events():
                position = (event.created_at, event.id)
                if page.position and position < page.position:
                    continue
                matches = [
                    key for key, value in sorted(event.overrides.items()) if _matches(value, query)
                ]
                if not _matches(event, query) and not matches:
                    continue
                fields = self._event_result(event)
                if matches:
                    fields["matching_occurrences"] = ", ".join(
                        occurrence_id(event.id, key) for key in matches
                    )
                yield position, "\n\n" + _fields_text(fields)

        return tool_success(page.read(records(), {"events": 0}, visible_length=_visible_length))

    def _find_free_time(self, arguments: JsonObject) -> JsonObject:
        page, start, end = self._page(arguments, days=7)
        assert start is not None and end is not None
        duration = page.arguments.get("duration", 60)

        def records() -> Iterator[Record]:
            lower = datetime.fromisoformat(page.position[0]) if page.position else start
            for slot in self._calendar.iter_free_slots(lower, end, duration, now_utc=page.now):
                minutes = int((slot.end_utc - slot.start_utc).total_seconds() // 60)
                yield (
                    (slot.start_utc.isoformat(),),
                    "\n" + f"{_local(slot.start_utc, self._zone())} to "
                    f"{_local(slot.end_utc, self._zone())} ({length_text(minutes)})",
                )

        data = {
            "free": 0,
            "window": _window_text(start, end, self._zone()),
            "timezone": self._calendar.system_timezone_name(),
        }
        result = page.read(records(), data, visible_length=_visible_length)
        if not result["free"]:
            result["note"] = f"No free span of {length_text(duration)} or more in this window."
        return tool_success(result)

    def _page(
        self, arguments: JsonObject, days: int | None = None
    ) -> tuple[CalendarPage, datetime | None, datetime | None]:
        identity = catalog_identity(
            self._calendar.list_events(), self._jobs, self._calendar.system_timezone_name()
        )
        page = CalendarPage.resume(arguments, self._now, identity)
        self._now = page.now
        if "cursor" not in arguments:
            page.offset = (
                1
                if arguments["action"] == "find_free_time"
                else (2 if arguments["action"] in {"list", "search"} else 0)
            )
        start = end = None
        if days is not None:
            start, end = self._window(page.arguments, days, now=page.now)
            if "cursor" in arguments and any(
                key in arguments for key in ("time_min", "time_max", WHEN_FIELD)
            ):
                repeated = {
                    **page.arguments,
                    **{key: value for key, value in arguments.items() if key != "cursor"},
                }
                if WHEN_FIELD in repeated:
                    repeated.pop("time_min", None)
                    repeated.pop("time_max", None)
                if self._window(repeated, days, now=page.now) != (start, end):
                    raise CalendarCallRefusedError(
                        "calendar was not run: cursor already retains the query's window. "
                        "Continue with only action and cursor, or start a new query "
                        "without cursor."
                    )
            page.arguments = {
                key: value
                for key, value in page.arguments.items()
                if key not in {"cursor", WHEN_FIELD}
            }
            page.arguments.update(time_min=start.isoformat(), time_max=end.isoformat())
        return page, start, end

    def _window(
        self, arguments: JsonObject, days: int, *, now: datetime
    ) -> tuple[datetime, datetime]:
        zone = self._zone()
        now = now.replace(second=0, microsecond=0)
        when = arguments.get(WHEN_FIELD)
        if isinstance(when, str):
            if "time_min" in arguments or "time_max" in arguments:
                raise CalendarCallRefusedError(
                    refusal(
                        "the window is given twice, as when and as time_min/time_max.", arguments
                    )
                )
            return parse_when(when, now_utc=now, tz=zone)
        start = _bound(arguments, "time_min", zone) or now
        local_end = start.astimezone(zone).replace(tzinfo=None) + timedelta(days=days)
        end = _bound(arguments, "time_max", zone) or local_end.replace(tzinfo=zone).astimezone(UTC)
        if end <= start:
            raise CalendarCallRefusedError(
                refusal(
                    "time_max must come after time_min.", arguments, time_max=STAND_INS["time_max"]
                )
            )
        return start, end

    def _event_result(self, event: CalendarEvent) -> JsonObject:
        data = self._event_fields(event)
        if event.recurring:
            data["next"] = self._next_text(event)
        return data

    def _next_text(self, event: CalendarEvent) -> str:
        items = islice(self._calendar.iter_occurrences(event, self._now), 3)
        return (
            ", ".join(f"{item.id} {_occurrence_start(item, event)}" for item in items)
            or "none ahead"
        )

    def _event_fields(self, event: CalendarEvent) -> JsonObject:
        if event.all_day:
            first, last = event.start, _end_text(event.end, all_day=True)
        else:
            zone = ZoneInfo(event.tz_name or self._calendar.system_timezone_name())
            start = datetime.fromisoformat(event.start)
            length = datetime.fromisoformat(event.end) - start
            initial, final = resolve_local_span(start, zone, length)
            first = _local(
                datetime.fromisoformat(event.start_utc) if event.start_utc else initial, zone
            )
            last = _local(datetime.fromisoformat(event.end_utc) if event.end_utc else final, zone)
        data: JsonObject = {"id": event.id, "title": event.title, "start": first, "end": last}
        if event.tz_name:
            data["timezone"] = event.tz_name
        for name in ("rrule", "location", "description"):
            if value := getattr(event, name):
                data[name] = value
        if jobs := self._jobs.get(event.id):
            data["cron_jobs"] = jobs
        return data

    def _occurrence_fields(self, item: EventOccurrence, event: CalendarEvent) -> JsonObject:
        data: JsonObject = {"id": item.id}
        if item.recurring:
            data["series"] = event.id
        data.update(
            title=item.title,
            start=_occurrence_start(item, event),
            end=(
                _end_text(item.end, all_day=True)
                if item.all_day
                else _local(
                    item.end_utc, ZoneInfo(event.tz_name or self._calendar.system_timezone_name())
                )
            ),
        )
        if event.tz_name:
            data["timezone"] = event.tz_name
        for name in ("location", "description"):
            if value := getattr(item, name):
                data[name] = value
        if jobs := self._jobs.get(event.id):
            data["cron_jobs"] = jobs
        return data

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self._calendar.system_timezone_name())


def _matches(value: Any, query: str) -> bool:
    return any(
        query
        in str(
            (value.get(name) if isinstance(value, dict) else getattr(value, name, None)) or ""
        ).casefold()
        for name in ("title", "description", "location")
    )


def _fields_text(fields: JsonObject) -> str:
    return "\n".join(f"{name}: {value}" for name, value in fields.items())


def _visible_length(data: JsonObject) -> int:
    return len(str(tool_result_text(json.dumps(tool_success(data), ensure_ascii=False))))


def _occurrence_start(item: EventOccurrence, event: CalendarEvent) -> str:
    return item.start if item.all_day else _local(item.start_utc, ZoneInfo(event.tz_name or "UTC"))


def _bound(arguments: JsonObject, name: str, zone: ZoneInfo) -> datetime | None:
    value = arguments.get(name)
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        raise _not_a_time(arguments, name) from None
    return (moment if moment.tzinfo else moment.replace(tzinfo=zone)).astimezone(UTC)


def _not_a_time(arguments: JsonObject, name: str) -> CalendarCallRefusedError:
    return CalendarCallRefusedError(
        refusal(
            f'{name} "{arguments[name]}" is not a date or local date-time.',
            arguments,
            **{name: STAND_INS[name]},
        )
    )


def _window_text(start: datetime, end: datetime, zone: ZoneInfo) -> str:
    first, last = start.astimezone(zone), end.astimezone(zone)
    if first.time() == time.min and last.time() == time.min:
        final = (last - timedelta(days=1)).date()
        return (
            first.date().isoformat()
            if final == first.date()
            else f"{first.date().isoformat()} to {final.isoformat()}"
        )
    return f"{_local(start, zone)} to {_local(end, zone)}"


def _local(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    return local.isoformat(timespec="seconds" if local.second else "minutes")


def _time_text(value: str) -> str:
    return value[:-3] if len(value) == 19 and value.endswith(":00") else value


def _end_text(end: str, *, all_day: bool) -> str:
    return (
        f"{end} (last day {(date.fromisoformat(end) - timedelta(days=1)).isoformat()})"
        if all_day
        else _time_text(end)
    )
