"""Repetition rules of calendar events: the stored RRULE text and how it expands."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.calendar import CalendarService, CalendarValidationError, occurrence_id


@pytest.fixture()
def service(tmp_path: Path) -> CalendarService:
    return CalendarService(tmp_path, tz="Europe/Berlin")


def _starts(service: CalendarService, lower: str, upper: str) -> list[str]:
    window_start, window_end = service.parse_window(lower, upper)
    return [item.start for item in service.occurrences_in_window(window_start, window_end)]


@pytest.mark.parametrize(
    ("rule", "stored"),
    [
        pytest.param("rrule:freq=weekly;byday=mo,we", "FREQ=WEEKLY;BYDAY=MO,WE", id="prefix-case"),
        pytest.param(" FREQ=DAILY; COUNT=3 ;", "FREQ=DAILY;COUNT=3", id="spaces"),
        pytest.param("BYDAY=1MO;FREQ=MONTHLY", "FREQ=MONTHLY;BYDAY=1MO", id="freq-first"),
        pytest.param("", None, id="empty"),
    ],
)
def test_rule_text_is_stored_in_one_form(
    service: CalendarService, rule: str, stored: str | None
) -> None:
    event = service.create_event(title="R", start="2030-01-07T09:00", rrule=rule)

    assert event.rrule == stored
    assert event.recurring is (stored is not None)


@pytest.mark.parametrize(
    ("start", "rule", "message"),
    [
        ("2030-01-10T09:00", "FREQ=HOURLY", "FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY"),
        ("2030-01-10T09:00", "FREQ=DAILY;BYSECOND=1", "part BYSECOND is not supported"),
        ("2030-01-10T09:00", "FREQ=DAILY;FREQ=WEEKLY", "gives FREQ twice"),
        ("2030-01-10T09:00", "FREQ", "must be NAME=VALUE"),
        ("2030-01-10T09:00", "FREQ=DAILY;INTERVAL=0", "INTERVAL must be a whole number"),
        ("2030-01-10T09:00", "FREQ=DAILY;COUNT=2;UNTIL=20300120", "COUNT or UNTIL, not both"),
        ("2030-01-10T09:00", "FREQ=DAILY;UNTIL=2030-01-20", "UNTIL must be a date such as"),
        ("2030-01-10T09:00", "FREQ=DAILY;BYDAY=XX", "rrule is not valid"),
        ("2030-01-10T09:00", "FREQ=DAILY;BYHOUR=1,2,3,4,5;BYMINUTE=0,10,20,30,40", "24 times"),
        ("2030-01-10", "FREQ=DAILY;BYHOUR=9", "an all-day event repeats on days"),
        # dateutil alone would look for a 30 February until the year 9999.
        ("2030-01-10T09:00", "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30", "produces no occurrence"),
        ("2030-01-10T09:00", "FREQ=DAILY;UNTIL=20300101", "before its first occurrence"),
    ],
)
def test_invalid_rule_is_refused_without_storing(
    service: CalendarService, start: str, rule: str, message: str
) -> None:
    with pytest.raises(CalendarValidationError, match=message):
        service.create_event(title="R", start=start, rrule=rule)
    assert service.list_events() == []


def test_timed_rule_keeps_its_wall_clock_time_across_dst(service: CalendarService) -> None:
    service.create_event(title="Standup", start="2026-10-19T09:00", rrule="FREQ=WEEKLY")
    window_start, window_end = service.parse_window("2026-10-19", "2026-11-02")

    occurrences = service.occurrences_in_window(window_start, window_end)

    assert [item.start for item in occurrences] == [
        "2026-10-19T09:00:00",
        "2026-10-26T09:00:00",
        "2026-11-02T09:00:00",
    ]
    assert [item.start_utc.hour for item in occurrences] == [7, 8, 8]


def test_start_the_clocks_skip_moves_forward_and_keeps_its_length(
    service: CalendarService,
) -> None:
    service.create_event(
        title="Daily", start="2026-03-28T02:30", end="2026-03-28T03:00", rrule="FREQ=DAILY;COUNT=3"
    )
    window_start, window_end = service.parse_window("2026-03-28", "2026-03-31")

    spans = [
        (item.start_utc, item.end_utc)
        for item in service.occurrences_in_window(window_start, window_end)
    ]

    assert spans == [
        (datetime(2026, 3, 28, 1, 30, tzinfo=UTC), datetime(2026, 3, 28, 2, 0, tzinfo=UTC)),
        (datetime(2026, 3, 29, 1, 30, tzinfo=UTC), datetime(2026, 3, 29, 2, 0, tzinfo=UTC)),
        (datetime(2026, 3, 30, 0, 30, tzinfo=UTC), datetime(2026, 3, 30, 1, 0, tzinfo=UTC)),
    ]


@pytest.mark.parametrize(
    ("until", "last"),
    [
        pytest.param("20300109", "2030-01-09T09:00:00", id="date-takes-the-whole-day"),
        pytest.param("20300109T085959", "2030-01-08T09:00:00", id="local-time"),
        pytest.param("20300109T080000Z", "2030-01-09T09:00:00", id="utc-time-in-event-zone"),
        pytest.param("20300109T075959Z", "2030-01-08T09:00:00", id="utc-time-before"),
    ],
)
def test_until_bounds_the_repetition_inclusively(
    service: CalendarService, until: str, last: str
) -> None:
    service.create_event(title="R", start="2030-01-07T09:00", rrule=f"FREQ=DAILY;UNTIL={until}")

    assert _starts(service, "2030-01-01", "2030-01-31")[-1] == last


def test_rule_parts_beyond_weekdays_expand(service: CalendarService) -> None:
    service.create_event(
        title="Last workday",
        start="2030-01-01T17:00",
        rrule="FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=2",
    )

    assert _starts(service, "2030-01-01", "2030-02-28") == [
        "2030-01-31T17:00:00",
        "2030-02-28T17:00:00",
    ]


def test_all_day_rule_repeats_whole_days(service: CalendarService) -> None:
    event = service.create_event(
        title="Course", start="2026-09-10", end="2026-09-12", rrule="FREQ=WEEKLY;BYDAY=TH"
    )
    asyncio.run(service.delete_occurrence(occurrence_id(event.id, "2026-09-17")))
    window_start, window_end = service.parse_window("2026-09-01", "2026-09-30")

    occurrences = service.occurrences_in_window(window_start, window_end)

    assert [(item.start, item.end) for item in occurrences] == [
        ("2026-09-10", "2026-09-12"),
        ("2026-09-24", "2026-09-26"),
    ]
    assert all(item.all_day for item in occurrences)
