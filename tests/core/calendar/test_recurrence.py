"""Tests for RFC 5545 recurrence normalization and expansion."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.calendar.errors import CalendarValidationError
from core.calendar.recurrence import (
    expand_recurring_allday,
    expand_recurring_timed,
    normalize_rrule,
    parse_date_string,
)

BERLIN = ZoneInfo("Europe/Berlin")
WEEKLY_MONDAY = {
    "freq": "weekly",
    "interval": 1,
    "count": None,
    "until": None,
    "by_weekday": ["mo"],
}


class TestNormalizeRrule:
    def test_none_stays_none(self) -> None:
        assert normalize_rrule(None) is None

    def test_normalizes_defaults(self) -> None:
        normalized = normalize_rrule({"freq": "weekly", "by_weekday": ["we", "mo"]})
        assert normalized == {
            "freq": "weekly",
            "interval": 1,
            "count": None,
            "until": None,
            "by_weekday": ["mo", "we"],
        }

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(CalendarValidationError, match="Unsupported rrule fields"):
            normalize_rrule({"freq": "daily", "bogus": 1})

    def test_rejects_unknown_freq(self) -> None:
        with pytest.raises(CalendarValidationError, match="rrule.freq"):
            normalize_rrule({"freq": "hourly"})

    def test_rejects_by_weekday_outside_weekly(self) -> None:
        with pytest.raises(CalendarValidationError, match="only valid for weekly"):
            normalize_rrule({"freq": "daily", "by_weekday": ["mo"]})

    def test_rejects_bad_interval(self) -> None:
        with pytest.raises(CalendarValidationError, match="interval"):
            normalize_rrule({"freq": "daily", "interval": 0})

    def test_rejects_bad_until(self) -> None:
        with pytest.raises(CalendarValidationError, match="rrule.until"):
            normalize_rrule({"freq": "daily", "until": "tomorrow"})


class TestParseDateString:
    def test_parses_iso_date(self) -> None:
        assert parse_date_string("2026-09-14", field_name="x").isoformat() == "2026-09-14"

    def test_rejects_datetime_string(self) -> None:
        with pytest.raises(CalendarValidationError):
            parse_date_string("2026-09-14T10:00:00", field_name="x")


class TestExpandRecurringTimed:
    def test_weekly_expansion_is_wall_clock_stable_across_dst(self) -> None:
        """09:00 Europe/Berlin stays 09:00 local when DST ends (UTC shifts +2 -> +1)."""
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 8, 31, 9, 0),
            tz=BERLIN,
            rrule_spec=dict(WEEKLY_MONDAY),
            duration_minutes=60,
            exdates=frozenset(),
            window_start_utc=datetime(2026, 10, 1, tzinfo=UTC),
            window_end_utc=datetime(2026, 11, 15, tzinfo=UTC),
            max_occurrences=500,
        )
        starts = [start.isoformat() for start, _ in occurrences]
        assert starts == [
            "2026-10-05T07:00:00+00:00",
            "2026-10-12T07:00:00+00:00",
            "2026-10-19T07:00:00+00:00",
            "2026-10-26T08:00:00+00:00",
            "2026-11-02T08:00:00+00:00",
            "2026-11-09T08:00:00+00:00",
        ]

    def test_occurrence_duration_keeps_wall_clock_shape(self) -> None:
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 8, 31, 9, 0),
            tz=BERLIN,
            rrule_spec=dict(WEEKLY_MONDAY),
            duration_minutes=60,
            exdates=frozenset(),
            window_start_utc=datetime(2026, 10, 26, tzinfo=UTC),
            window_end_utc=datetime(2026, 10, 27, tzinfo=UTC),
            max_occurrences=500,
        )
        start_utc, end_utc = occurrences[0]
        assert start_utc == datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
        assert end_utc == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)

    def test_exdates_remove_single_occurrences(self) -> None:
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 8, 31, 9, 0),
            tz=BERLIN,
            rrule_spec=dict(WEEKLY_MONDAY),
            duration_minutes=60,
            exdates=frozenset({"2026-10-12T09:00:00"}),
            window_start_utc=datetime(2026, 10, 1, tzinfo=UTC),
            window_end_utc=datetime(2026, 11, 15, tzinfo=UTC),
            max_occurrences=500,
        )
        assert len(occurrences) == 5

    def test_count_limits_occurrences_from_dtstart(self) -> None:
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 9, 7, 9, 0),
            tz=BERLIN,
            rrule_spec={
                "freq": "daily",
                "interval": 1,
                "count": 3,
                "until": None,
                "by_weekday": None,
            },
            duration_minutes=30,
            exdates=frozenset(),
            window_start_utc=datetime(2026, 9, 1, tzinfo=UTC),
            window_end_utc=datetime(2026, 10, 1, tzinfo=UTC),
            max_occurrences=500,
        )
        assert [start.date().isoformat() for start, _ in occurrences] == [
            "2026-09-07",
            "2026-09-08",
            "2026-09-09",
        ]

    def test_until_bounds_recurrence_inclusively(self) -> None:
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 9, 7, 9, 0),
            tz=BERLIN,
            rrule_spec={
                "freq": "daily",
                "interval": 1,
                "count": None,
                "until": "2026-09-08",
                "by_weekday": None,
            },
            duration_minutes=30,
            exdates=frozenset(),
            window_start_utc=datetime(2026, 9, 1, tzinfo=UTC),
            window_end_utc=datetime(2026, 10, 1, tzinfo=UTC),
            max_occurrences=500,
        )
        assert [start.date().isoformat() for start, _ in occurrences] == [
            "2026-09-07",
            "2026-09-08",
        ]

    def test_max_occurrences_caps_expansion(self) -> None:
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 9, 1, 0, 0),
            tz=BERLIN,
            rrule_spec={
                "freq": "daily",
                "interval": 1,
                "count": None,
                "until": None,
                "by_weekday": None,
            },
            duration_minutes=15,
            exdates=frozenset(),
            window_start_utc=datetime(2026, 9, 1, tzinfo=UTC),
            window_end_utc=datetime(2026, 10, 1, tzinfo=UTC),
            max_occurrences=4,
        )
        assert len(occurrences) == 4

    def test_occurrence_starting_before_window_still_overlaps(self) -> None:
        """A long occurrence that started before the window is included."""
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 9, 28, 20, 0),
            tz=BERLIN,
            rrule_spec={
                "freq": "monthly",
                "interval": 1,
                "count": None,
                "until": None,
                "by_weekday": None,
            },
            duration_minutes=60 * 24,
            exdates=frozenset(),
            window_start_utc=datetime(2026, 9, 29, 0, 0, tzinfo=UTC),
            window_end_utc=datetime(2026, 9, 30, 0, 0, tzinfo=UTC),
            max_occurrences=500,
        )
        assert len(occurrences) == 1


class TestExpandRecurringAllday:
    def test_weekly_allday_expansion_overlaps_window(self) -> None:
        occurrences = expand_recurring_allday(
            start_date=date(2026, 9, 10),
            duration_days=2,
            rrule_spec={
                "freq": "daily",
                "interval": 7,
                "count": None,
                "until": None,
                "by_weekday": None,
            },
            exdates=frozenset(),
            window_start_utc=datetime(2026, 9, 1, tzinfo=UTC),
            window_end_utc=datetime(2026, 10, 1, tzinfo=UTC),
            system_tz=BERLIN,
            max_occurrences=500,
        )
        assert (date(2026, 9, 10), date(2026, 9, 12)) in occurrences
        assert (date(2026, 9, 17), date(2026, 9, 19)) in occurrences
        assert all(end - start == timedelta(days=2) for start, end in occurrences)

    def test_exdates_remove_dates(self) -> None:
        occurrences = expand_recurring_allday(
            start_date=date(2026, 9, 10),
            duration_days=1,
            rrule_spec={
                "freq": "weekly",
                "interval": 1,
                "count": None,
                "until": None,
                "by_weekday": ["th"],
            },
            exdates=frozenset({"2026-09-17"}),
            window_start_utc=datetime(2026, 9, 1, tzinfo=UTC),
            window_end_utc=datetime(2026, 10, 1, tzinfo=UTC),
            system_tz=BERLIN,
            max_occurrences=500,
        )
        starts = [start.isoformat() for start, _ in occurrences]
        assert "2026-09-10" in starts
        assert "2026-09-17" not in starts
