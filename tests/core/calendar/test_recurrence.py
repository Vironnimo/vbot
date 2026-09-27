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
    def test_normalizes_defaults(self) -> None:
        normalized = normalize_rrule({"freq": "weekly", "by_weekday": ["we", "mo"]})
        assert normalized == {
            "freq": "weekly",
            "interval": 1,
            "count": None,
            "until": None,
            "by_weekday": ["mo", "we"],
        }

    @pytest.mark.parametrize(
        ("rrule", "message"),
        [
            ({"freq": "daily", "bogus": 1}, "Unsupported rrule fields"),
            ({"freq": "hourly"}, "rrule.freq"),
            ({"freq": "daily", "by_weekday": ["mo"]}, "only valid for weekly"),
            ({"freq": "daily", "interval": 0}, "interval"),
            ({"freq": "daily", "until": "2026-09-14T10:00:00"}, "rrule.until must be a date"),
            ({"freq": "daily", "count": 2, "until": "2026-09-14"}, "either count or until"),
        ],
        ids=[
            "unknown-field",
            "unknown-freq",
            "weekday-outside-weekly",
            "interval",
            "until-is-a-datetime",
            "count-and-until",
        ],
    )
    def test_rejects_invalid_rules(self, rrule: dict[str, object], message: str) -> None:
        with pytest.raises(CalendarValidationError, match=message):
            normalize_rrule(rrule)


class TestExpandRecurringTimed:
    def test_gap_start_shifts_forward_without_losing_duration_or_count(self):
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 3, 28, 2, 30),
            tz=BERLIN,
            rrule_spec={"freq": "daily", "interval": 1, "count": 3},
            duration_minutes=30,
            exdates=frozenset(),
            window_start_utc=datetime(2026, 3, 28, tzinfo=UTC),
            window_end_utc=datetime(2026, 3, 31, tzinfo=UTC),
            max_occurrences=500,
        )
        assert [start for start, _ in occurrences] == [
            datetime(2026, 3, 28, 1, 30, tzinfo=UTC),
            datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
            datetime(2026, 3, 30, 0, 30, tzinfo=UTC),
        ]
        assert all(end - start == timedelta(minutes=30) for start, end in occurrences)

    def test_fall_back_overlap_is_filtered_by_instants(self):
        occurrences = expand_recurring_timed(
            start_local=datetime(2026, 10, 25, 2, 30),
            tz=BERLIN,
            rrule_spec={"freq": "daily", "interval": 1, "count": 1},
            duration_minutes=30,
            exdates=frozenset(),
            window_start_utc=datetime(2026, 10, 25, 1, 10, tzinfo=UTC),
            window_end_utc=datetime(2026, 10, 25, 1, 20, tzinfo=UTC),
            max_occurrences=500,
        )
        assert occurrences == [
            (datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 25, 2, 0, tzinfo=UTC))
        ]

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
        # The one-hour duration keeps its shape on both sides of the change.
        assert all(end - start == timedelta(hours=1) for start, end in occurrences)
        assert starts == [
            "2026-10-05T07:00:00+00:00",
            "2026-10-12T07:00:00+00:00",
            "2026-10-19T07:00:00+00:00",
            "2026-10-26T08:00:00+00:00",
            "2026-11-02T08:00:00+00:00",
            "2026-11-09T08:00:00+00:00",
        ]

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
    def test_multi_day_occurrences_overlap_the_window_minus_exdates(self) -> None:
        occurrences = expand_recurring_allday(
            start_date=date(2026, 9, 10),
            duration_days=2,
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
        # 2026-10-01 starts at 22:00 UTC the day before, inside the UTC window.
        assert occurrences == [
            (date(2026, 9, 10), date(2026, 9, 12)),
            (date(2026, 9, 24), date(2026, 9, 26)),
            (date(2026, 10, 1), date(2026, 10, 3)),
        ]
