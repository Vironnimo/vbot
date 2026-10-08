"""The calendar Tool accepts the calendar dialects Models know; reminders get cron's call.

Calls go through production dispatch; each accepted dialect must reach the same state as the
canonical call, and each refusal names the corrected call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.calendar import CalendarEvent
from tests.core.tools.scheduling_tool_support import CalendarTool, calendar_tool, clock_at


@pytest.fixture
def tool(tmp_path: Path) -> CalendarTool:
    return calendar_tool(tmp_path)


def _fields(event: CalendarEvent) -> dict[str, Any]:
    return {
        "title": event.title,
        "start": event.start,
        "end": event.end,
        "description": event.description,
        "rrule": event.rrule,
    }


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (
            {
                "action": "create",
                "summary": "Dentist",
                "start": "2030-01-10T15:00",
                "notes": "Card",
            },
            {"title": "Dentist", "description": "Card"},
        ),
        (
            {
                "action": "create",
                "subject": "Dentist",
                "start": "2030-01-10T15:00",
                "details": "Card",
            },
            {"title": "Dentist", "description": "Card"},
        ),
        (
            {"action": "create", "name": "Dentist", "start": "2030-01-10T15:00", "body": "Card"},
            {"title": "Dentist", "description": "Card"},
        ),
        (
            {
                "action": "create",
                "summary": "Dentist",
                "start": {"dateTime": "2030-01-10T15:00:00", "timeZone": "Europe/Berlin"},
                "end": {"dateTime": "2030-01-10T16:30:00", "timeZone": "Europe/Berlin"},
            },
            {"title": "Dentist", "end": "2030-01-10T16:30:00"},
        ),
        (
            # A time in another zone becomes the same instant in server time.
            {
                "action": "create",
                "summary": "Dentist",
                "start": {"dateTime": "2030-01-10T09:00:00", "timeZone": "America/New_York"},
            },
            {"title": "Dentist"},
        ),
        (
            {
                "action": "create",
                "summary": "Holiday",
                "start": {"date": "2030-01-10"},
                "end": {"date": "2030-01-13"},
            },
            {"title": "Holiday", "start": "2030-01-10", "end": "2030-01-13"},
        ),
        (
            {
                "action": "create",
                "title": "Dentist",
                "start": "2030-01-10T15:00",
                "duration": "1.5h",
            },
            {"end": "2030-01-10T16:30:00"},
        ),
        (
            {"action": "create", "title": "Dentist", "start": "2030-01-10T15:00", "duration": 90},
            {"end": "2030-01-10T16:30:00"},
        ),
        (
            {
                "action": "create",
                "title": "Standup",
                "start": "2030-01-07T09:00",
                "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
            },
            {
                "title": "Standup",
                "start": "2030-01-07T09:00:00",
                "end": "2030-01-07T10:00:00",
                "rrule": "FREQ=WEEKLY;BYDAY=MO",
            },
        ),
        (
            {
                "action": "create",
                "title": "Standup",
                "start": "2030-01-07T09:00",
                "rrule": {"freq": "weekly", "by_weekday": ["MO"]},
            },
            {
                "title": "Standup",
                "start": "2030-01-07T09:00:00",
                "end": "2030-01-07T10:00:00",
                "rrule": "FREQ=WEEKLY;BYDAY=MO",
            },
        ),
        (
            # Fields that ask for nothing this calendar lacks are dropped.
            {
                "action": "create",
                "calendarId": "primary",
                "title": "Dentist",
                "start": "2030-01-10T15:00",
                "sendUpdates": "none",
                "colorId": "5",
                "reminders": {"useDefault": True},
            },
            {"title": "Dentist"},
        ),
    ],
)
def test_create_dialects_reach_the_canonical_event(
    tool: CalendarTool, call: dict[str, Any], expected: dict[str, Any]
) -> None:
    tool.succeeded(call)

    canonical = {
        "title": "Dentist",
        "start": "2030-01-10T15:00:00",
        "end": "2030-01-10T16:00:00",
        "description": None,
        "rrule": None,
    }
    assert _fields(tool.only_event()) == {**canonical, **expected}


@pytest.mark.parametrize(
    "call",
    [
        {
            "action": "update",
            "event_id": "{series}",
            "originalStartTime": {"dateTime": "2030-01-09T09:00:00+01:00"},
            "summary": "Moved",
        },
        {
            "action": "update",
            "id": "{series}",
            "original_start": "2030-01-09T09:00",
            "title": "Moved",
        },
        {"action": "update", "id": "{series}_20300109T0900", "title": "Moved"},
    ],
)
def test_original_start_with_the_series_id_names_the_occurrence(
    tool: CalendarTool, call: dict[str, Any]
) -> None:
    series = tool.service.create_event(
        title="Standup", start="2030-01-07T09:00", rrule="FREQ=WEEKLY;BYDAY=MO,WE"
    ).id
    filled = {
        key: value.replace("{series}", series) if isinstance(value, str) else value
        for key, value in call.items()
    }

    tool.succeeded(filled)

    event = tool.only_event()
    assert (event.title, event.overrides) == (
        "Standup",
        {"2030-01-09T09:00:00": {"title": "Moved"}},
    )


@pytest.mark.parametrize(
    ("call", "window"),
    [
        (
            {"action": "list", "timeMin": "2030-01-06", "timeMax": "2030-01-13"},
            "2030-01-06 to 2030-01-12",
        ),
        ({"action": "list", "from": "2030-01-06", "to": "2030-01-13"}, "2030-01-06 to 2030-01-12"),
        ({"action": "list", "when": "2030-01"}, "2030-01-01 to 2030-01-31"),
        ({"action": "list", "when": "2030-01-06..2030-01-12"}, "2030-01-06 to 2030-01-12"),
        ({"action": "list", "when": "this week"}, "2030-01-07 to 2030-01-13"),
        ({"action": "list", "when": "today"}, "2030-01-08"),
        ({"action": "list", "when": "next month"}, "2030-02-01 to 2030-02-28"),
    ],
)
def test_window_dialects_set_the_window(
    tool: CalendarTool, monkeypatch: pytest.MonkeyPatch, call: dict[str, Any], window: str
) -> None:
    now = datetime(2030, 1, 8, 12, tzinfo=UTC)
    monkeypatch.setattr("core.tools.calendar.datetime", clock_at(now))

    assert f"window: {window}" in tool.succeeded(call)


@pytest.mark.parametrize(
    "call",
    [
        {"action": "list", "q": "dentist", "time_min": "2030-01-01"},
        {"action": "list", "search": "dentist", "time_min": "2030-01-01"},
        {"action": "search_events", "query": "dentist", "timeMin": "2030-01-01"},
    ],
)
def test_query_dialects_search(tool: CalendarTool, call: dict[str, Any]) -> None:
    tool.service.create_event(title="Dentist", start="2030-01-10T15:00")
    tool.service.create_event(title="Piano", start="2030-01-10T17:00")

    text = tool.succeeded(call)

    assert "events: 1" in text and "title: Dentist" in text


@pytest.mark.parametrize("action", ["find_free", "freebusy"])
def test_free_time_dialects_find_free_time(tool: CalendarTool, action: str) -> None:
    text = tool.succeeded(
        {
            "action": action,
            "timeMin": "2030-01-07T08:00",
            "timeMax": "2030-01-07T10:00",
            "duration": 30,
        }
    )

    assert text.splitlines()[0] == "free: 1"


@pytest.mark.parametrize(
    ("call", "event_call", "schedule"),
    [
        (
            {"reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}},
            '{"action":"create","title":"Call mom","start":"2030-01-11T18:00"}',
            "start - 30m",
        ),
        (
            {"minutes_before": 15},
            '{"action":"create","title":"Call mom","start":"2030-01-11T18:00"}',
            "start - 15m",
        ),
        (
            {"action": "add_reminder", "prompt": "Remind me to call mom."},
            '{"action":"create","title":"Call mom","start":"2030-01-11T18:00"}',
            "<start or end, e.g. start - 30m>",
        ),
    ],
)
def test_a_reminder_is_refused_with_the_cron_call(
    tool: CalendarTool, call: dict[str, Any], event_call: str, schedule: str
) -> None:
    message = tool.refused(
        {"action": "create", "title": "Call mom", "start": "2030-01-11T18:00", **call}
    )

    prompt = call.get("prompt", "<instruction>")
    assert message == (
        "calendar was not run: the calendar has no reminders. cron runs an instruction before, at "
        f"or after an event. Send: {event_call} Then send to cron: "
        '{"action":"create","event_id":"<event id from the result>",'
        f'"prompt":"{prompt}","schedule":"{schedule}"}}'
    )


def test_a_reminder_on_an_occurrence_binds_cron_to_the_series(tool: CalendarTool) -> None:
    series = tool.service.create_event(
        title="Standup", start="2030-01-07T09:00", rrule="FREQ=WEEKLY;BYDAY=MO"
    ).id

    message = tool.refused(
        {"action": "update", "id": f"{series}_20300114T0900", "minutes_before": 10}
    )

    assert message == (
        "calendar was not run: the calendar has no reminders. cron runs an instruction before, at "
        f'or after an event. Send to cron: {{"action":"create","event_id":"{series}",'
        '"prompt":"<instruction>","schedule":"start - 10m"}'
    )


def test_without_cron_a_reminder_is_refused_without_naming_it(tmp_path: Path) -> None:
    tool = calendar_tool(tmp_path, cron_offered=False)

    message = tool.refused(
        {"action": "create", "title": "Call mom", "start": "2030-01-11T18:00", "minutes_before": 30}
    )

    assert message == (
        "calendar was not run: the calendar has no reminders. Send: "
        '{"action":"create","title":"Call mom","start":"2030-01-11T18:00"}'
    )


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            {
                "action": "create",
                "title": "X",
                "start": "2030-01-10T15:00",
                "attendees": [{"email": "ann@example.com"}],
            },
            "calendar was not run: the calendar cannot invite attendees. To record them, put them "
            'in description. Send: {"action":"create","title":"X","start":"2030-01-10T15:00",'
            '"description":"Attendees: ann@example.com"}',
        ),
        (
            {"action": "list", "calendarId": "work"},
            'calendar was not run: there is one local calendar; "calendarId" "work" cannot select '
            'another. Send: {"action":"list"}',
        ),
        (
            {"action": "update", "id": "evt_1", "rrule": ["FREQ=DAILY", "FREQ=WEEKLY"]},
            "calendar was not run: rrule takes one rule such as FREQ=WEEKLY;BYDAY=MO. Send: "
            '{"action":"update","id":"evt_1","rrule":["FREQ=DAILY","FREQ=WEEKLY"]}',
        ),
        (
            {"action": "update", "id": "evt_1", "duration": 30},
            "calendar was not run: duration counts from start, which this call does not send; "
            'send end instead. Send: {"action":"update","id":"evt_1","end":"<same form as start>"}',
        ),
        (
            {
                "action": "create",
                "title": "X",
                "start": "2030-01-10T15:00",
                "end": "2030-01-10T16:00",
                "duration": 30,
            },
            'calendar was not run: "end" and "duration" give different ends: {"action":"create",'
            '"title":"X","start":"2030-01-10T15:00","end":"2030-01-10T16:00"} or '
            '{"action":"create","title":"X","start":"2030-01-10T15:00","end":"2030-01-10T15:30"}',
        ),
        (
            {"action": "create", "id": "evt_1", "title": "X", "start": "2030-01-10T15:00"},
            'calendar was not run: create makes a new event and takes no id; to change "evt_1" use '
            'update: {"action":"update","id":"evt_1","title":"X","start":"2030-01-10T15:00"} or '
            '{"action":"create","title":"X","start":"2030-01-10T15:00"}',
        ),
        (
            {"action": "delete", "id": "evt_1", "title": "X"},
            "calendar was not run: delete removes the event, but the call also sends changes: "
            '{"action":"delete","id":"evt_1"} or {"action":"update","id":"evt_1","title":"X"}',
        ),
    ],
)
def test_calls_with_different_readings_are_refused_with_the_corrected_call(
    tool: CalendarTool, call: dict[str, Any], message: str
) -> None:
    assert tool.refused(call) == message
