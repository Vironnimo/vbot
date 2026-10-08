"""Tests for the local calendar service: events, occurrences, expansion and storage."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

import pytest

from core.calendar import (
    CalendarEventNotFoundError,
    CalendarService,
    CalendarStorageError,
    CalendarValidationError,
    occurrence_id,
    parse_occurrence_id,
    validate_calendar_events_file,
)


@pytest.fixture()
def service(tmp_path: Path) -> CalendarService:
    return CalendarService(tmp_path, tz="Europe/Berlin")


def _window(service: CalendarService, lower: str, upper: str) -> list[Any]:
    window_start, window_end = service.parse_window(lower, upper)
    return service.occurrences_in_window(window_start, window_end)


@pytest.mark.parametrize("start", ["2026-09-03", "2026-09-03T09:00:00"])
def test_occurrence_limit_applies_to_each_event(service, start):
    events = [
        service.create_event(title=title, start=start, rrule="FREQ=DAILY")
        for title in ("First", "Second")
    ]
    occurrences = service.occurrences_in_window(
        datetime(2026, 9, 3, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC), max_per_event=2
    )
    assert len(occurrences) == 4
    for event in events:
        assert sum(item.event_id == event.id for item in occurrences) == 2


@pytest.mark.parametrize("limit", [0, True, 1.5])
def test_invalid_occurrence_limit_is_rejected(service, limit):
    with pytest.raises(CalendarValidationError):
        service.occurrences_in_window(
            datetime(2026, 9, 3, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC), max_per_event=limit
        )


class TestCreateEvent:
    @pytest.mark.parametrize(
        "start", ["2026-09-03T15:00", "2026-09-03T15:00:00+02:00", "2026-09-03T13:00:00Z"]
    )
    def test_timed_event_keeps_local_times_in_the_server_zone(
        self, service: CalendarService, start: str
    ) -> None:
        event = service.create_event(title="Dentist", start=start)

        assert (event.start, event.end, event.tz_name) == (
            "2026-09-03T15:00:00",
            "2026-09-03T16:00:00",
            "Europe/Berlin",
        )
        assert (event.all_day, event.recurring, event.description, event.location) == (
            False,
            False,
            None,
            None,
        )

    def test_end_description_and_location_are_kept(self, service: CalendarService) -> None:
        event = service.create_event(
            title=" Review ",
            start="2026-09-03T15:00",
            end="2026-09-03T17:30",
            description="Bring the draft.\nPrint it.",
            location="  Room 4 ",
        )

        assert (event.title, event.end, event.description, event.location) == (
            "Review",
            "2026-09-03T17:30:00",
            "Bring the draft.\nPrint it.",
            "Room 4",
        )

    @pytest.mark.parametrize(
        ("end", "stored_end"),
        [
            pytest.param(None, "2026-09-15", id="default-one-day"),
            pytest.param("2026-09-14", "2026-09-15", id="end-on-start-day-is-one-day"),
            pytest.param("2026-09-17", "2026-09-17", id="exclusive-end"),
        ],
    )
    def test_all_day_event_has_an_exclusive_end_date(
        self, service: CalendarService, end: str | None, stored_end: str
    ) -> None:
        event = service.create_event(title="Holiday", start="2026-09-14", end=end)

        assert (event.all_day, event.tz_name, event.start, event.end) == (
            True,
            None,
            "2026-09-14",
            stored_end,
        )

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            pytest.param({"title": "  "}, "title", id="empty-title"),
            pytest.param({"start": "next tuesday"}, "start", id="invalid-start"),
            pytest.param({"end": "2026-09-14T08:00"}, "end must be after start", id="end-first"),
            pytest.param({"end": "2026-09-15"}, "end must be a date-time", id="end-kind"),
            pytest.param({"end": "2026-10-20T09:00"}, "at most 30 days", id="too-long"),
            pytest.param({"start": "0999-01-01T09:00"}, "between the years", id="year"),
            pytest.param({"location": "x" * 501}, "location must not exceed", id="location"),
        ],
    )
    def test_rejects_invalid_fields(
        self, service: CalendarService, fields: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(CalendarValidationError, match=message):
            service.create_event(**{"title": "X", "start": "2026-09-14T09:00", **fields})
        assert service.list_events() == []

    def test_rejects_unknown_constructor_timezone(self, tmp_path: Path) -> None:
        with pytest.raises(CalendarValidationError, match="IANA"):
            CalendarService(tmp_path, tz="Mars/Olympus")

    def test_events_keep_the_zone_of_their_creation(self, tmp_path: Path) -> None:
        service = CalendarService(tmp_path, tz="UTC")
        before = service.create_event(title="Before", start="2026-01-15T09:00:00")

        service.set_timezone("Europe/Berlin")
        after = service.create_event(title="After", start="2026-01-15T09:00:00")

        assert service.system_timezone_name() == "Europe/Berlin"
        assert service.get_event(before.id).tz_name == "UTC"
        assert after.tz_name == "Europe/Berlin"
        starts = {
            item.title: item.start_utc for item in _window(service, "2026-01-15", "2026-01-15")
        }
        assert starts == {
            "Before": datetime(2026, 1, 15, 9, tzinfo=UTC),
            "After": datetime(2026, 1, 15, 8, tzinfo=UTC),
        }

    def test_capacity_limit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        service = CalendarService(tmp_path)
        from core.calendar.service import MAX_CALENDAR_EVENTS

        monkeypatch.setattr(service, "_save_events", lambda: None)
        for index in range(MAX_CALENDAR_EVENTS):
            service.create_event(title=f"e{index}", start="2026-09-14")
        with pytest.raises(CalendarValidationError, match="at most"):
            service.create_event(title="overflow", start="2026-09-14")


class TestUpdateEvent:
    def test_update_changes_only_provided_fields(
        self, service: CalendarService, caplog: pytest.LogCaptureFixture
    ) -> None:
        event = service.create_event(
            title="Standup", start="2026-08-31T09:00:00", location="Hall", rrule="FREQ=WEEKLY"
        )
        with caplog.at_level(logging.INFO, logger="vbot.calendar.service"):
            updated = asyncio.run(
                service.update_event(event.id, title="Daily", end="2026-08-31T09:15")
            )
        assert (updated.title, updated.start, updated.end) == (
            "Daily",
            "2026-08-31T09:00:00",
            "2026-08-31T09:15:00",
        )
        assert (updated.location, updated.rrule) == ("Hall", "FREQ=WEEKLY")
        # The log names the changed fields, never the title itself.
        [message] = [r.getMessage() for r in caplog.records if r.name == "vbot.calendar.service"]
        assert "title" in message
        assert "Daily" not in message

    @pytest.mark.parametrize(
        ("created", "start", "expected"),
        [
            pytest.param(
                ("2026-09-03T15:00", "2026-09-03T17:30"),
                "2026-09-04T08:00",
                ("2026-09-04T08:00:00", "2026-09-04T10:30:00"),
                id="timed-keeps-length",
            ),
            pytest.param(
                ("2026-09-14", "2026-09-17"),
                "2026-09-20",
                ("2026-09-20", "2026-09-23"),
                id="all-day-keeps-length",
            ),
            pytest.param(
                ("2026-09-14", "2026-09-17"),
                "2026-09-20T10:00",
                ("2026-09-20T10:00:00", "2026-09-20T11:00:00"),
                id="kind-switch-takes-the-default",
            ),
        ],
    )
    def test_new_start_alone_moves_the_end(
        self,
        service: CalendarService,
        created: tuple[str, str],
        start: str,
        expected: tuple[str, str],
    ) -> None:
        event = service.create_event(title="X", start=created[0], end=created[1])

        updated = asyncio.run(service.update_event(event.id, start=start))

        assert (updated.start, updated.end) == expected

    def test_moving_a_series_moves_its_changed_and_removed_occurrences(
        self, service: CalendarService
    ) -> None:
        event = service.create_event(title="Weekly", start="2030-01-07T09:00", rrule="FREQ=WEEKLY")
        asyncio.run(service.delete_occurrence(occurrence_id(event.id, "2030-01-14T09:00:00")))
        asyncio.run(
            service.update_occurrence(occurrence_id(event.id, "2030-01-21T09:00:00"), title="Kept")
        )

        moved = asyncio.run(service.update_event(event.id, start="2030-01-08T10:00"))

        assert moved.exdates == ["2030-01-15T10:00:00"]
        assert moved.overrides == {"2030-01-22T10:00:00": {"title": "Kept"}}
        assert [
            (item.start, item.title) for item in _window(service, "2030-01-01", "2030-01-31")
        ] == [
            ("2030-01-08T10:00:00", "Weekly"),
            ("2030-01-22T10:00:00", "Kept"),
            ("2030-01-29T10:00:00", "Weekly"),
        ]

    def test_new_rule_drops_the_changes_of_occurrences_it_no_longer_has(
        self, service: CalendarService
    ) -> None:
        event = service.create_event(title="Weekly", start="2030-01-07T09:00", rrule="FREQ=WEEKLY")
        asyncio.run(service.delete_occurrence(occurrence_id(event.id, "2030-01-14T09:00:00")))
        asyncio.run(
            service.update_occurrence(occurrence_id(event.id, "2030-01-21T09:00:00"), title="Kept")
        )

        changed = asyncio.run(service.update_event(event.id, rrule="FREQ=WEEKLY;INTERVAL=2"))

        assert changed.exdates == []
        assert changed.overrides == {"2030-01-21T09:00:00": {"title": "Kept"}}

    @pytest.mark.parametrize("rrule", [None, ""])
    def test_stopping_repetition_drops_occurrence_changes(
        self, service: CalendarService, rrule: str | None
    ) -> None:
        event = service.create_event(title="Standup", start="2026-08-31T09:00", rrule="FREQ=WEEKLY")
        asyncio.run(service.delete_occurrence(occurrence_id(event.id, "2026-09-14T09:00:00")))

        updated = asyncio.run(service.update_event(event.id, rrule=rrule))

        assert (updated.rrule, updated.exdates, updated.overrides) == (None, [], {})
        assert updated.start == "2026-08-31T09:00:00"

    def test_update_rejects_unknown_fields(self, service: CalendarService) -> None:
        event = service.create_event(title="X", start="2026-09-14")
        with pytest.raises(CalendarValidationError, match="Unsupported"):
            asyncio.run(service.update_event(event.id, bogus=1))

    def test_update_missing_event(self, service: CalendarService) -> None:
        with pytest.raises(CalendarEventNotFoundError):
            asyncio.run(service.update_event("missing", title="X"))


class TestDeleteEvent:
    def test_delete_removes_event(self, service: CalendarService) -> None:
        event = service.create_event(title="X", start="2026-09-14")
        asyncio.run(service.delete_event(event.id))
        assert service.list_events() == []

    def test_delete_missing_event(self, service: CalendarService) -> None:
        with pytest.raises(CalendarEventNotFoundError):
            asyncio.run(service.delete_event("nope"))


class TestOccurrences:
    def test_occurrence_ids_name_the_original_start(self, service: CalendarService) -> None:
        weekly = service.create_event(title="W", start="2030-01-07T09:00", rrule="FREQ=WEEKLY")
        daily = service.create_event(title="D", start="2030-01-07", rrule="FREQ=DAILY;COUNT=2")
        single = service.create_event(title="S", start="2030-01-08T12:00")

        ids = [item.id for item in _window(service, "2030-01-07", "2030-01-08")]

        assert ids == [
            f"{daily.id}_20300107",
            f"{weekly.id}_20300107T0900",
            f"{daily.id}_20300108",
            single.id,
        ]
        assert parse_occurrence_id(f"{weekly.id}_20300107T0900") == (
            weekly.id,
            "2030-01-07T09:00:00",
        )
        assert parse_occurrence_id(single.id) is None
        occurrence = service.get_occurrence(f"{weekly.id}_20300107T0900")
        assert (occurrence.event_id, occurrence.original_start, occurrence.overridden) == (
            weekly.id,
            "2030-01-07T09:00:00",
            False,
        )
        assert service.get_occurrence(single.id).original_start is None

    def test_update_occurrence_changes_only_that_occurrence(self, service: CalendarService) -> None:
        event = service.create_event(
            title="Standup", start="2030-01-07T09:00", end="2030-01-07T09:30", rrule="FREQ=WEEKLY"
        )
        second = occurrence_id(event.id, "2030-01-14T09:00:00")

        changed = asyncio.run(
            service.update_occurrence(
                second, title="Planning", location="Hall", start="2030-01-15T14:00"
            )
        )

        assert (changed.id, changed.title, changed.location) == (second, "Planning", "Hall")
        assert (changed.start, changed.end, changed.overridden) == (
            "2030-01-15T14:00:00",
            "2030-01-15T14:30:00",
            True,
        )
        assert service.get_event(event.id).overrides == {
            "2030-01-14T09:00:00": {
                "title": "Planning",
                "location": "Hall",
                "start": "2030-01-15T14:00:00",
                "end": "2030-01-15T14:30:00",
            }
        }
        assert [
            (item.start, item.title) for item in _window(service, "2030-01-07", "2030-01-21")
        ] == [
            ("2030-01-07T09:00:00", "Standup"),
            ("2030-01-15T14:00:00", "Planning"),
            ("2030-01-21T09:00:00", "Standup"),
        ]

    def test_changing_an_occurrence_back_drops_its_change(self, service: CalendarService) -> None:
        event = service.create_event(title="Standup", start="2030-01-07T09:00", rrule="FREQ=WEEKLY")
        second = occurrence_id(event.id, "2030-01-14T09:00:00")
        asyncio.run(service.update_occurrence(second, title="Other", start="2030-01-14T10:00"))

        asyncio.run(service.update_occurrence(second, title="Standup", start="2030-01-14T09:00"))

        assert service.get_event(event.id).overrides == {}
        assert service.get_occurrence(second).overridden is False

    def test_occurrence_keeps_the_event_kind(self, service: CalendarService) -> None:
        event = service.create_event(title="Standup", start="2030-01-07T09:00", rrule="FREQ=WEEKLY")

        with pytest.raises(CalendarValidationError, match="keeps the event's kind"):
            asyncio.run(
                service.update_occurrence(
                    occurrence_id(event.id, "2030-01-14T09:00:00"), start="2030-01-14"
                )
            )

    def test_delete_occurrence_removes_it_with_its_change(self, service: CalendarService) -> None:
        event = service.create_event(title="Standup", start="2030-01-07T09:00", rrule="FREQ=WEEKLY")
        second = occurrence_id(event.id, "2030-01-14T09:00:00")
        asyncio.run(service.update_occurrence(second, title="Other"))

        removed = asyncio.run(service.delete_occurrence(second))

        stored = service.get_event(event.id)
        assert (removed.id, removed.title) == (second, "Other")
        assert (stored.exdates, stored.overrides) == (["2030-01-14T09:00:00"], {})
        assert [item.start for item in _window(service, "2030-01-07", "2030-01-21")] == [
            "2030-01-07T09:00:00",
            "2030-01-21T09:00:00",
        ]
        with pytest.raises(CalendarEventNotFoundError):
            service.get_occurrence(second)

    def test_ids_that_name_no_occurrence_are_not_found(self, service: CalendarService) -> None:
        weekly = service.create_event(title="W", start="2030-01-07T09:00", rrule="FREQ=WEEKLY")
        single = service.create_event(title="S", start="2030-01-08T12:00")

        for missing in (
            weekly.id,
            f"{weekly.id}_20300108T0900",
            f"{single.id}_20300108T1200",
            "evt_missing_20300107",
        ):
            with pytest.raises(CalendarEventNotFoundError):
                service.get_occurrence(missing)
            with pytest.raises(CalendarEventNotFoundError):
                asyncio.run(service.delete_occurrence(missing))
        with pytest.raises(CalendarEventNotFoundError):
            asyncio.run(service.update_occurrence(single.id, title="X"))

    def test_iter_occurrences_runs_lazily_in_start_order(self, service: CalendarService) -> None:
        event = service.create_event(title="Daily", start="2030-01-07T09:00", rrule="FREQ=DAILY")
        asyncio.run(
            service.update_occurrence(
                occurrence_id(event.id, "2030-01-08T09:00:00"), start="2030-01-09T12:00"
            )
        )
        asyncio.run(service.delete_occurrence(occurrence_id(event.id, "2030-01-10T09:00:00")))
        stored = service.get_event(event.id)

        after = datetime(2030, 1, 7, 12, tzinfo=UTC)
        first = list(islice(service.iter_occurrences(stored, after), 3))

        assert [(item.original_start, item.start) for item in first] == [
            ("2030-01-09T09:00:00", "2030-01-09T09:00:00"),
            ("2030-01-08T09:00:00", "2030-01-09T12:00:00"),
            ("2030-01-11T09:00:00", "2030-01-11T09:00:00"),
        ]


class TestOccurrencesInWindow:
    @pytest.mark.parametrize("recurring", [False, True])
    @pytest.mark.parametrize("day", ["2026-09-03", "2026-03-29", "2026-10-25"])
    def test_allday_blocks_intraday_window(self, service, recurring, day):
        event = service.create_event(
            title="Busy", start=day, rrule="FREQ=DAILY;COUNT=1" if recurring else None
        )
        start, end = service.parse_window(f"{day}T09:00:00", f"{day}T17:00:00")
        assert [item.event_id for item in service.occurrences_in_window(start, end)] == [event.id]
        assert service.find_free_slots(start, end, 30, now_utc=start) == []
        midnight, next_midnight = service.parse_window(day, day)
        assert service.occurrences_in_window(midnight - timedelta(hours=1), midnight) == []
        assert (
            service.occurrences_in_window(next_midnight, next_midnight + timedelta(hours=1)) == []
        )

    def test_shifted_dst_occurrence_can_be_excluded(self, service):
        service.create_event(
            title="Daily",
            start="2026-03-28T02:30:00",
            end="2026-03-28T03:00:00",
            rrule="FREQ=DAILY;COUNT=3",
        )
        start, end = service.parse_window("2026-03-29T03:35:00", "2026-03-29T03:45:00")
        (occurrence,) = service.occurrences_in_window(start, end)
        assert occurrence.start_utc == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
        assert occurrence.end_utc == datetime(2026, 3, 29, 2, 0, tzinfo=UTC)
        assert service.find_free_slots(start, end, 5, now_utc=start) == []
        asyncio.run(service.delete_occurrence(occurrence.id))
        assert service.occurrences_in_window(start, end) == []

    def test_single_and_recurring_and_allday_expand(self, service: CalendarService) -> None:
        service.create_event(title="Single", start="2026-09-03T15:00:00+02:00")
        service.create_event(title="Weekly", start="2026-09-07T09:00:00", rrule="FREQ=WEEKLY")
        service.create_event(title="Urlaub", start="2026-09-14", end="2026-09-17")

        occurrences = _window(service, "2026-09-01", "2026-09-30")

        titles = [occurrence.title for occurrence in occurrences]
        assert (titles.count("Weekly"), titles.count("Single"), titles.count("Urlaub")) == (4, 1, 1)
        weekly = next(item for item in occurrences if item.title == "Weekly")
        assert (weekly.start, weekly.end, weekly.recurring) == (
            "2026-09-07T09:00:00",
            "2026-09-07T10:00:00",
            True,
        )
        urlaub = next(item for item in occurrences if item.title == "Urlaub")
        assert (urlaub.start, urlaub.end, urlaub.all_day) == ("2026-09-14", "2026-09-17", True)
        assert urlaub.start_utc == datetime(2026, 9, 13, 22, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("fields", "removed", "instant", "next_start"),
        [
            # Berlin midnight on 2026-09-14 is 22:00 UTC the day before.
            pytest.param(
                {"start": "2026-09-14"}, [], "2026-09-13T21:59", "2026-09-13T22:00", id="ahead"
            ),
            pytest.param({"start": "2026-09-14"}, [], "2026-09-13T22:01", None, id="started"),
            pytest.param(
                {"start": "2026-09-14", "rrule": "FREQ=DAILY;COUNT=3"},
                [],
                "2026-09-16T12:00",
                None,
                id="series-ended",
            ),
            pytest.param(
                {"start": "2026-09-14", "rrule": "FREQ=DAILY;COUNT=3"},
                ["2026-09-16"],
                "2026-09-15T12:00",
                None,
                id="rest-removed",
            ),
            pytest.param(
                {"start": "2026-09-07T09:00:00", "rrule": "FREQ=WEEKLY;UNTIL=20260921"},
                [],
                "2026-09-21T06:59",
                "2026-09-21T07:00",
                id="last-occurrence-ahead",
            ),
            pytest.param(
                {"start": "2026-09-07T09:00:00", "rrule": "FREQ=MONTHLY"},
                [],
                "2031-01-01T00:00",
                "2031-01-07T08:00",
                id="series-without-end",
            ),
        ],
    )
    def test_next_start_follows_expansion(
        self,
        service: CalendarService,
        fields: dict[str, Any],
        removed: list[str],
        instant: str,
        next_start: str | None,
    ) -> None:
        event = service.create_event(title="Event", **fields)
        for key in removed:
            asyncio.run(service.delete_occurrence(occurrence_id(event.id, key)))
        event = service.get_event(event.id)

        at = datetime.fromisoformat(instant).replace(tzinfo=UTC)
        expected = datetime.fromisoformat(next_start).replace(tzinfo=UTC) if next_start else None
        assert service.next_start(event, at) == expected

    def test_rejects_inverted_window(self, service: CalendarService) -> None:
        with pytest.raises(CalendarValidationError, match="after"):
            service.occurrences_in_window(
                datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)
            )

    def test_rejects_oversized_window(self, service: CalendarService) -> None:
        from core.calendar.service import MAX_WINDOW_DAYS

        with pytest.raises(CalendarValidationError, match="window span"):
            service.occurrences_in_window(
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=MAX_WINDOW_DAYS + 1),
            )


class TestFindFreeSlots:
    @pytest.mark.parametrize("end_minute,second,expected", [(2, 0, 5), (5, 0, 5), (5, 1, 10)])
    def test_rounds_cursor_after_busy_interval(self, service, end_minute, second, expected):
        service.create_event(title="Busy", start=f"2026-09-03T09:{end_minute:02}:{second:02}+00:00")
        start = datetime(2026, 9, 3, 9, 30, tzinfo=UTC)
        end = datetime(2026, 9, 3, 11, tzinfo=UTC)
        slots = service.find_free_slots(start, end, 30, now_utc=start)
        assert slots[0].start_utc == datetime(2026, 9, 3, 10, expected, tzinfo=UTC)

    def test_first_read_after_restart_uses_persisted_events(self, tmp_path: Path) -> None:
        original = CalendarService(tmp_path, tz="Europe/Berlin")
        original.create_event(title="All day", start="2026-09-03")
        restarted = CalendarService(tmp_path, tz="Europe/Berlin")
        start, end = restarted.parse_window("2026-09-03", "2026-09-03")

        assert restarted.find_free_slots(start, end, 60, now_utc=start) == []

    def test_slots_are_whole_gaps_around_events(self, service: CalendarService) -> None:
        service.create_event(title="Block", start="2026-09-03T15:00:00+02:00")
        window_start, window_end = service.parse_window("2026-09-03", "2026-09-03")

        slots = service.find_free_slots(window_start, window_end, 60, now_utc=window_start)

        # Local midnight to 15:00 and 16:00 to midnight, not one-hour pieces.
        assert [(slot.start_utc, slot.end_utc) for slot in slots] == [
            (datetime(2026, 9, 2, 22, 0, tzinfo=UTC), datetime(2026, 9, 3, 13, 0, tzinfo=UTC)),
            (datetime(2026, 9, 3, 14, 0, tzinfo=UTC), datetime(2026, 9, 3, 22, 0, tzinfo=UTC)),
        ]

    def test_gaps_shorter_than_the_duration_are_skipped(self, service: CalendarService) -> None:
        service.create_event(title="A", start="2026-09-03T09:00:00+00:00")
        service.create_event(title="B", start="2026-09-03T10:30:00+00:00")
        window_start = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
        window_end = datetime(2026, 9, 3, 13, 0, tzinfo=UTC)

        slots = service.find_free_slots(window_start, window_end, 45, now_utc=window_start)

        assert [(slot.start_utc, slot.end_utc) for slot in slots] == [
            (datetime(2026, 9, 3, 11, 30, tzinfo=UTC), window_end)
        ]

    @pytest.mark.parametrize(
        ("reference_now", "first_start"),
        [
            pytest.param(
                datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
                datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
                id="exact-boundary",
            ),
            pytest.param(
                datetime(2026, 9, 3, 10, 0, 1, tzinfo=UTC),
                datetime(2026, 9, 3, 10, 5, tzinfo=UTC),
                id="rounds-up-to-next-five-minutes",
            ),
        ],
    )
    def test_first_slot_starts_at_reference_now_rounded_up(
        self, service: CalendarService, reference_now: datetime, first_start: datetime
    ) -> None:
        window_start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
        window_end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
        slots = service.find_free_slots(window_start, window_end, 60, now_utc=reference_now)
        assert slots[0].start_utc == first_start

    def test_rejects_bad_duration(self, service: CalendarService) -> None:
        window_start, window_end = service.parse_window("2026-09-03", "2026-09-04")
        with pytest.raises(CalendarValidationError, match="duration_minutes"):
            service.find_free_slots(window_start, window_end, 0)


class TestParseWindow:
    def test_date_bounds_span_local_days(self, service: CalendarService) -> None:
        """A date bound selects its whole local day, so to is inclusive."""
        window_start, window_end = service.parse_window("2026-09-03", "2026-09-03")
        assert window_end - window_start == timedelta(days=1)
        window_start, window_end = service.parse_window("2026-09-03", "2026-09-04")
        assert window_end - window_start == timedelta(days=2)

    def test_datetime_bounds_are_absolute(self, service: CalendarService) -> None:
        window_start, window_end = service.parse_window(
            "2026-09-03T08:00:00+00:00", "2026-09-03T10:00:00+00:00"
        )
        assert window_start == datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
        assert window_end == datetime(2026, 9, 3, 10, 0, tzinfo=UTC)

    def test_rejects_end_before_start(self, service: CalendarService) -> None:
        with pytest.raises(CalendarValidationError, match="after"):
            service.parse_window("2026-09-04", "2026-09-03")


def _events_path(root: Path) -> Path:
    return root / "calendar" / "events.json"


class TestPersistence:
    @pytest.mark.parametrize(
        ("field", "value", "path"),
        [
            pytest.param("rrule", {"freq": "daily"}, "$.events[0].rrule", id="rule-object"),
            pytest.param("rrule", "FREQ=HOURLY", "$.events[0]", id="rule-text"),
            pytest.param("start", "soon", "$.events[0]", id="start"),
            pytest.param("tz_name", "Mars/Olympus", "$.events[0]", id="zone"),
            pytest.param(
                "overrides",
                {"2026-09-15T09:00:00": {"title": 5}},
                "$.events[0].overrides",
                id="change",
            ),
        ],
    )
    def test_invalid_stored_event_is_reported_isolated_and_kept(
        self, tmp_path: Path, field: str, value: Any, path: str
    ) -> None:
        service = CalendarService(tmp_path, tz="UTC")
        broken = service.create_event(title="Broken", start="2026-09-14T09:00", rrule="FREQ=DAILY")
        valid = service.create_event(title="Valid", start="2026-09-14")
        payload = json.loads(_events_path(tmp_path).read_text(encoding="utf-8"))
        invalid_entry = next(entry for entry in payload["events"] if entry["id"] == broken.id)
        invalid_entry[field] = value
        invalid_entry["future_field"] = {"retained": True}
        _events_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

        report = validate_calendar_events_file(_events_path(tmp_path))
        errors = [item.path for item in report.diagnostics if item.severity == "error"]
        assert errors and all(item.startswith(path) for item in errors)
        restarted = CalendarService(tmp_path, tz="UTC")
        assert [event.id for event in restarted.list_events()] == [valid.id]
        assert [item.event_id for item in _window(restarted, "2026-09-14", "2026-09-14")] == [
            valid.id
        ]

        asyncio.run(restarted.update_event(valid.id, title="Updated"))
        rewritten = json.loads(_events_path(tmp_path).read_text(encoding="utf-8"))
        assert next(entry for entry in rewritten["events"] if entry["id"] == broken.id) == (
            invalid_entry
        )

    def test_events_survive_restart_and_saves_keep_unknown_fields(self, tmp_path: Path) -> None:
        service = CalendarService(tmp_path, tz="Europe/Berlin")
        event = service.create_event(
            title="Standup", start="2026-08-31T09:00:00", rrule="FREQ=WEEKLY;BYDAY=MO"
        )
        asyncio.run(
            service.update_occurrence(occurrence_id(event.id, "2026-09-07T09:00:00"), title="Demo")
        )
        raw = json.loads(_events_path(tmp_path).read_text(encoding="utf-8"))
        raw["future_setting"] = "kept"
        raw["events"][0]["color"] = "blue"
        _events_path(tmp_path).write_text(json.dumps(raw), encoding="utf-8")

        reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
        assert reloaded.get_event(event.id) == service.get_event(event.id)
        asyncio.run(reloaded.update_event(event.id, title="Weekly standup"))

        rewritten = json.loads(_events_path(tmp_path).read_text(encoding="utf-8"))
        assert rewritten["format_version"] == 2
        assert rewritten["future_setting"] == "kept"
        stored = rewritten["events"][0]
        assert (stored["title"], stored["color"], stored["rrule"]) == (
            "Weekly standup",
            "blue",
            "FREQ=WEEKLY;BYDAY=MO",
        )
        assert stored["overrides"] == {"2026-09-07T09:00:00": {"title": "Demo"}}

    def test_events_of_an_earlier_version_are_ignored_and_replaced(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        old_event = {"id": "evt_old", "title": "Old", "start_utc": "2026-09-14T10:00:00+00:00"}
        _events_path(tmp_path).parent.mkdir(parents=True)
        _events_path(tmp_path).write_text(
            json.dumps({"format_version": 1, "events": [old_event]}), encoding="utf-8"
        )

        report = validate_calendar_events_file(_events_path(tmp_path))
        assert [(item.severity, item.path) for item in report.diagnostics] == [
            ("warning", "$.format_version")
        ]
        service = CalendarService(tmp_path)
        with caplog.at_level(logging.WARNING, logger="vbot.calendar.service"):
            assert service.list_events() == []
        assert "earlier vBot version" in caplog.text
        new = service.create_event(title="New", start="2026-09-15")

        rewritten = json.loads(_events_path(tmp_path).read_text(encoding="utf-8"))
        assert rewritten["format_version"] == 2
        assert [entry["id"] for entry in rewritten["events"]] == [new.id]

    @pytest.mark.parametrize(
        ("content", "message", "denied"),
        [
            pytest.param(
                json.dumps({"format_version": 3, "events": []}),
                "written by a newer vBot",
                False,
                id="newer-format",
            ),
            pytest.param("{not an array", "Invalid JSON", False, id="malformed"),
            # A file that cannot be checked is not missing: it is never seeded over.
            pytest.param(
                json.dumps({"format_version": 2, "events": []}),
                "Cannot initialize calendar storage",
                True,
                id="access-denied",
            ),
        ],
    )
    def test_unreadable_storage_reads_empty_and_is_never_overwritten(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        deny_access: Callable[[Path], None],
        content: str,
        message: str,
        denied: bool,
    ) -> None:
        events_path = _events_path(tmp_path)
        events_path.parent.mkdir(parents=True)
        events_path.write_text(content, encoding="utf-8")
        if denied:
            deny_access(events_path.parent)
        service = CalendarService(tmp_path)

        assert service.list_events() == []
        with pytest.raises(CalendarStorageError, match=message):
            service.create_event(title="X", start="2026-09-14")
        monkeypatch.undo()
        assert events_path.read_text(encoding="utf-8") == content

    def test_changed_callback_fires_on_mutation(self, tmp_path: Path) -> None:
        service = CalendarService(tmp_path)
        calls: list[int] = []
        unsubscribe = service.add_changed_callback(lambda: calls.append(1))
        event = service.create_event(title="X", start="2026-09-14", rrule="FREQ=DAILY")
        asyncio.run(service.update_event(event.id, title="Y"))
        asyncio.run(service.update_occurrence(occurrence_id(event.id, "2026-09-15"), title="Z"))
        asyncio.run(service.delete_occurrence(occurrence_id(event.id, "2026-09-16")))
        asyncio.run(service.delete_event(event.id))
        assert len(calls) == 5
        unsubscribe()
        service.create_event(title="Y", start="2026-09-15")
        assert len(calls) == 5


def test_short_event_ids_skip_collisions_after_reload(tmp_path, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    service = CalendarService(tmp_path, tz="Europe/Berlin")
    first = service.create_event(title="first", start="2026-09-07")
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
    second = reloaded.create_event(title="second", start="2026-09-07")
    assert first.id == "evt_000000000001"
    assert second.id == "evt_000000000002"
    assert reloaded.get_event(first.id).title == "first"
