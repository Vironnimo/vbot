"""Tests for the agent-facing `when` window parser."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from core.calendar.errors import CalendarValidationError
from core.calendar.when import WHEN_GRAMMAR, parse_when

BERLIN = ZoneInfo("Europe/Berlin")
# Wednesday, 2026-09-02 14:00 UTC = 16:00 Berlin (CEST).
NOW = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)


def _parse(value: str) -> tuple[datetime, datetime]:
    return parse_when(value, now_utc=NOW, tz=BERLIN)


class TestNamedSpans:
    def test_today_is_the_current_local_day(self) -> None:
        start, end = _parse("today")
        assert (start, end) == (
            datetime(2026, 9, 1, 22, 0, tzinfo=UTC),
            datetime(2026, 9, 2, 22, 0, tzinfo=UTC),
        )

    def test_tomorrow_is_the_next_local_day(self) -> None:
        start, end = _parse("tomorrow")
        assert (start, end) == (
            datetime(2026, 9, 2, 22, 0, tzinfo=UTC),
            datetime(2026, 9, 3, 22, 0, tzinfo=UTC),
        )

    def test_this_week_runs_monday_to_monday(self) -> None:
        start, end = _parse("this week")
        assert (start, end) == (
            datetime(2026, 8, 30, 22, 0, tzinfo=UTC),
            datetime(2026, 9, 6, 22, 0, tzinfo=UTC),
        )

    def test_next_week_starts_next_monday_even_from_monday(self) -> None:
        monday_now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
        start, end = parse_when("next week", now_utc=monday_now, tz=BERLIN)
        assert (start, end) == (
            datetime(2026, 9, 6, 22, 0, tzinfo=UTC),
            datetime(2026, 9, 13, 22, 0, tzinfo=UTC),
        )

    def test_this_month_spans_the_whole_month(self) -> None:
        start, end = _parse("this month")
        assert (start, end) == (
            datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
            datetime(2026, 9, 30, 22, 0, tzinfo=UTC),
        )

    def test_next_month_rolls_over_december(self) -> None:
        december_now = datetime(2026, 12, 10, 12, 0, tzinfo=UTC)
        start, end = parse_when("next month", now_utc=december_now, tz=BERLIN)
        assert (start, end) == (
            datetime(2026, 12, 31, 23, 0, tzinfo=UTC),
            datetime(2027, 1, 31, 23, 0, tzinfo=UTC),
        )

    def test_matching_is_case_insensitive(self) -> None:
        start, _end = _parse("Today")
        assert start == datetime(2026, 9, 1, 22, 0, tzinfo=UTC)


class TestDateForms:
    def test_date_is_that_whole_local_day(self) -> None:
        start, end = _parse("2026-09-15")
        assert (start, end) == (
            datetime(2026, 9, 14, 22, 0, tzinfo=UTC),
            datetime(2026, 9, 15, 22, 0, tzinfo=UTC),
        )

    def test_year_month_is_the_whole_month(self) -> None:
        start, end = _parse("2026-12")
        assert (start, end) == (
            datetime(2026, 11, 30, 23, 0, tzinfo=UTC),
            datetime(2026, 12, 31, 23, 0, tzinfo=UTC),
        )

    def test_date_window_stays_wall_clock_across_dst_end(self) -> None:
        """2026-10-25 is the Berlin DST transition; the day is 23 hours long."""
        start, end = _parse("2026-10-25")
        assert (start, end) == (
            datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
            datetime(2026, 10, 25, 23, 0, tzinfo=UTC),
        )


class TestRanges:
    def test_date_range_end_day_is_inclusive(self) -> None:
        start, end = _parse("2026-09-10..2026-09-14")
        assert (start, end) == (
            datetime(2026, 9, 9, 22, 0, tzinfo=UTC),
            datetime(2026, 9, 14, 22, 0, tzinfo=UTC),
        )

    def test_datetime_bounds_are_absolute(self) -> None:
        start, end = _parse("2026-09-10T08:00..2026-09-10T18:00")
        assert (start, end) == (
            datetime(2026, 9, 10, 6, 0, tzinfo=UTC),
            datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
        )

    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(CalendarValidationError, match="after its start"):
            _parse("2026-09-14..2026-09-10")


class TestInvalidInput:
    def test_rejects_unknown_expression(self) -> None:
        with pytest.raises(CalendarValidationError, match="cannot parse when"):
            _parse("someday")

    def test_rejects_empty_value(self) -> None:
        with pytest.raises(CalendarValidationError, match="must not be empty"):
            _parse("  ")

    def test_rejects_non_string_value(self) -> None:
        with pytest.raises(CalendarValidationError):
            parse_when(123, now_utc=NOW, tz=BERLIN)  # type: ignore[arg-type]

    def test_error_names_the_grammar(self) -> None:
        with pytest.raises(CalendarValidationError) as error:
            _parse("someday")
        assert WHEN_GRAMMAR in str(error.value)

    def test_rejects_invalid_month_number(self) -> None:
        with pytest.raises(CalendarValidationError, match="cannot parse when"):
            _parse("2026-13")
