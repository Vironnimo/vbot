"""Tests for the agent-facing `when` window parser."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from core.calendar.errors import CalendarValidationError
from core.calendar.when import WHEN_GRAMMAR, parse_when

BERLIN = ZoneInfo("Europe/Berlin")
# Wednesday, 2026-09-02 14:00 UTC = 16:00 Berlin (CEST).
NOW = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
MONDAY = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
DECEMBER = datetime(2026, 12, 10, 12, 0, tzinfo=UTC)


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "now", "window"),
    [
        # Named spans are local calendar units.
        pytest.param("today", NOW, (_utc(2026, 9, 1, 22), _utc(2026, 9, 2, 22)), id="today"),
        pytest.param("Today", NOW, (_utc(2026, 9, 1, 22), _utc(2026, 9, 2, 22)), id="case"),
        pytest.param("tomorrow", NOW, (_utc(2026, 9, 2, 22), _utc(2026, 9, 3, 22)), id="tomorrow"),
        pytest.param(
            "this week", NOW, (_utc(2026, 8, 30, 22), _utc(2026, 9, 6, 22)), id="monday-to-monday"
        ),
        pytest.param(
            "next week",
            MONDAY,
            (_utc(2026, 9, 6, 22), _utc(2026, 9, 13, 22)),
            id="next-week-from-monday",
        ),
        pytest.param(
            "this month", NOW, (_utc(2026, 8, 31, 22), _utc(2026, 9, 30, 22)), id="this-month"
        ),
        pytest.param(
            "next month",
            DECEMBER,
            (_utc(2026, 12, 31, 23), _utc(2027, 1, 31, 23)),
            id="next-month-rolls-over-december",
        ),
        # Date forms are whole local days or months.
        pytest.param("2026-09-15", NOW, (_utc(2026, 9, 14, 22), _utc(2026, 9, 15, 22)), id="date"),
        pytest.param(
            "2026-12", NOW, (_utc(2026, 11, 30, 23), _utc(2026, 12, 31, 23)), id="year-month"
        ),
        # 2026-10-25 is the Berlin DST end; that day is 23 hours long.
        pytest.param(
            "2026-10-25", NOW, (_utc(2026, 10, 24, 22), _utc(2026, 10, 25, 23)), id="dst-end-day"
        ),
        # Ranges: a date range includes its end day; datetime bounds are absolute.
        pytest.param(
            "2026-09-10..2026-09-14",
            NOW,
            (_utc(2026, 9, 9, 22), _utc(2026, 9, 14, 22)),
            id="date-range",
        ),
        pytest.param(
            "2026-09-10T08:00..2026-09-10T18:00",
            NOW,
            (_utc(2026, 9, 10, 6), _utc(2026, 9, 10, 16)),
            id="local-datetime-range",
        ),
        pytest.param(
            "2026-09-10T08:00Z..2026-09-10T18:00Z",
            NOW,
            (_utc(2026, 9, 10, 8), _utc(2026, 9, 10, 18)),
            id="utc-datetime-range",
        ),
    ],
)
def test_when_resolves_to_a_utc_window(
    value: str, now: datetime, window: tuple[datetime, datetime]
) -> None:
    assert parse_when(value, now_utc=now, tz=BERLIN) == window


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param("someday", "cannot parse when", id="unknown-expression"),
        pytest.param("2026-13", "cannot parse when", id="invalid-month"),
        pytest.param("  ", "must not be empty", id="empty"),
        pytest.param(123, "must not be empty", id="not-a-string"),
        pytest.param("2026-09-14..2026-09-10", "after its start", id="inverted-range"),
        # Both bounds fall in the spring DST gap; resolving them inverts the range.
        pytest.param(
            "2026-03-29T02:30..2026-03-29T03:15", "after its start", id="inverted-after-gap"
        ),
    ],
)
def test_invalid_when_is_rejected(value: Any, message: str) -> None:
    with pytest.raises(CalendarValidationError, match=message) as error:
        parse_when(value, now_utc=NOW, tz=BERLIN)

    # Every rejection teaches the grammar.
    assert WHEN_GRAMMAR in str(error.value)
