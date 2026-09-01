"""Tests for the local calendar service."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from core.calendar import (
    CalendarEventNotFoundError,
    CalendarService,
    CalendarStorageError,
    CalendarValidationError,
)


@pytest.fixture()
def service(tmp_path: Path) -> CalendarService:
    return CalendarService(tmp_path, tz="Europe/Berlin")


class TestCreateEvent:
    def test_single_timed_event_stores_utc_instant(self, service: CalendarService) -> None:
        event = service.create_event(title="Zahnarzt", start="2026-09-03T15:00:00+02:00")
        assert event.start_utc == "2026-09-03T13:00:00+00:00"
        assert event.start_local is None
        assert event.tz_name is None
        assert event.duration_minutes == 60

    def test_recurring_timed_event_anchors_to_wall_clock(self, service: CalendarService) -> None:
        event = service.create_event(
            title="Standup",
            start="2026-08-31T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        assert event.start_local == "2026-08-31T09:00:00"
        assert event.tz_name == "Europe/Berlin"
        assert event.rrule is not None
        assert event.start_utc is None

    def test_recurring_with_offset_normalizes_into_target_zone(
        self, service: CalendarService
    ) -> None:
        event = service.create_event(
            title="Sync",
            start="2026-08-31T09:00:00+02:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        assert event.start_local == "2026-08-31T09:00:00"
        assert event.tz_name == "Europe/Berlin"

    def test_date_only_start_creates_all_day_event(self, service: CalendarService) -> None:
        event = service.create_event(title="Urlaub", start="2026-09-14", duration_days=3)
        assert event.all_day is True
        assert event.start_date == "2026-09-14"
        assert event.duration_days == 3

    def test_rejects_date_only_start_with_all_day_false(self, service: CalendarService) -> None:
        with pytest.raises(CalendarValidationError, match="all_day"):
            service.create_event(title="X", start="2026-09-14", all_day=False)

    def test_rejects_datetime_start_with_all_day_true(self, service: CalendarService) -> None:
        with pytest.raises(CalendarValidationError, match="all_day"):
            service.create_event(title="X", start="2026-09-14T10:00:00+00:00", all_day=True)

    def test_rejects_unknown_constructor_timezone(self, tmp_path: Path) -> None:
        with pytest.raises(CalendarValidationError, match="IANA"):
            CalendarService(tmp_path, tz="Mars/Olympus")

    def test_timezone_change_applies_to_future_local_events(self, tmp_path: Path) -> None:
        service = CalendarService(tmp_path, tz="UTC")

        service.set_timezone("Europe/Berlin")
        event = service.create_event(title="Local", start="2026-01-15T09:00:00")

        assert service.system_timezone_name() == "Europe/Berlin"
        assert event.start_utc == "2026-01-15T08:00:00+00:00"

    def test_rejects_exdates_on_single_event(self, service: CalendarService) -> None:
        with pytest.raises(CalendarValidationError, match="exdates"):
            service.create_event(
                title="X", start="2026-09-14T10:00:00+00:00", exdates=["2026-09-14"]
            )

    def test_rejects_empty_title(self, service: CalendarService) -> None:
        with pytest.raises(CalendarValidationError, match="title"):
            service.create_event(title="  ", start="2026-09-14")

    def test_rejects_invalid_start(self, service: CalendarService) -> None:
        with pytest.raises(CalendarValidationError, match="start"):
            service.create_event(title="X", start="next tuesday")

    def test_capacity_limit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        service = CalendarService(tmp_path)
        from core.calendar.service import MAX_CALENDAR_EVENTS

        monkeypatch.setattr(service, "_save_events", lambda: None)
        for index in range(MAX_CALENDAR_EVENTS):
            service.create_event(title=f"e{index}", start="2026-09-14")
        with pytest.raises(CalendarValidationError, match="at most"):
            service.create_event(title="overflow", start="2026-09-14")


class TestUpdateEvent:
    def test_update_changes_only_provided_fields(self, service: CalendarService) -> None:
        event = service.create_event(
            title="Standup",
            start="2026-08-31T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        updated = service.update_event(event.id, title="Daily", duration_minutes=15)
        assert updated.title == "Daily"
        assert updated.duration_minutes == 15
        assert updated.start_local == "2026-08-31T09:00:00"
        assert updated.rrule == event.rrule

    def test_update_can_clear_recurrence(self, service: CalendarService) -> None:
        event = service.create_event(
            title="Standup",
            start="2026-08-31T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        updated = service.update_event(event.id, rrule=None)
        assert updated.rrule is None
        assert updated.start_utc == "2026-08-31T07:00:00+00:00"
        assert updated.exdates == []

    def test_update_can_clear_recurrence_dropping_exdates(self, service: CalendarService) -> None:
        event = service.create_event(
            title="Standup",
            start="2026-08-31T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        service.add_exdate(event.id, "2026-09-14T09:00:00")
        updated = service.update_event(event.id, rrule=None)
        assert updated.rrule is None
        assert updated.exdates == []
        assert updated.start_utc == "2026-08-31T07:00:00+00:00"

    def test_update_rejects_unknown_fields(self, service: CalendarService) -> None:
        event = service.create_event(title="X", start="2026-09-14")
        with pytest.raises(CalendarValidationError, match="Unsupported"):
            service.update_event(event.id, bogus=1)

    def test_update_missing_event(self, service: CalendarService) -> None:
        with pytest.raises(CalendarEventNotFoundError):
            service.update_event("missing", title="X")


class TestDeleteEvent:
    def test_delete_removes_event(self, service: CalendarService) -> None:
        event = service.create_event(title="X", start="2026-09-14")
        service.delete_event(event.id)
        assert service.list_events() == []

    def test_delete_missing_event(self, service: CalendarService) -> None:
        with pytest.raises(CalendarEventNotFoundError):
            service.delete_event("nope")


class TestAddExdate:
    def test_adds_normalized_exdate(self, service: CalendarService) -> None:
        event = service.create_event(
            title="Standup",
            start="2026-08-31T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        updated = service.add_exdate(event.id, "2026-10-12T09:00:00")
        assert updated.exdates == ["2026-10-12T09:00:00"]

    def test_rejects_exdate_on_single_event(self, service: CalendarService) -> None:
        event = service.create_event(title="X", start="2026-09-14T10:00:00+00:00")
        with pytest.raises(CalendarValidationError, match="Single events"):
            service.add_exdate(event.id, "2026-09-14")

    def test_rejects_offset_exdate_for_timed_event(self, service: CalendarService) -> None:
        event = service.create_event(
            title="Standup",
            start="2026-08-31T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        with pytest.raises(CalendarValidationError, match="naive local"):
            service.add_exdate(event.id, "2026-10-12T09:00:00+02:00")


class TestOccurrencesInWindow:
    def test_single_and_recurring_and_allday_expand(self, service: CalendarService) -> None:
        service.create_event(title="Single", start="2026-09-03T15:00:00+02:00", duration_minutes=60)
        service.create_event(
            title="Weekly",
            start="2026-09-07T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        service.create_event(title="Urlaub", start="2026-09-14", duration_days=3)
        window_start, window_end = service.parse_window("2026-09-01", "2026-09-30")
        occurrences = service.occurrences_in_window(window_start, window_end)
        titles = [occurrence.title for occurrence in occurrences]
        assert titles.count("Weekly") == 4
        assert titles.count("Single") == 1
        weekly = [occurrence for occurrence in occurrences if occurrence.title == "Weekly"]
        assert weekly[0].occurrence_start == "2026-09-07T09:00:00"
        assert weekly[0].occurrence_end == "2026-09-07T10:00:00"
        urlaub = [occurrence for occurrence in occurrences if occurrence.title == "Urlaub"]
        assert urlaub[0].start_date == date(2026, 9, 14)
        assert urlaub[0].end_date == date(2026, 9, 17)
        assert urlaub[0].occurrence_start == "2026-09-14"
        assert urlaub[0].occurrence_end is None

    def test_excluded_occurrence_does_not_expand(self, service: CalendarService) -> None:
        event = service.create_event(
            title="Weekly",
            start="2026-09-07T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        service.add_exdate(event.id, "2026-09-14T09:00:00")
        window_start, window_end = service.parse_window("2026-09-01", "2026-09-30")
        occurrences = service.occurrences_in_window(window_start, window_end)
        assert len(occurrences) == 3

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
    def test_finds_gaps_around_events(self, service: CalendarService) -> None:
        service.create_event(title="Block", start="2026-09-03T15:00:00+02:00", duration_minutes=60)
        window_start, window_end = service.parse_window("2026-09-03", "2026-09-04")
        slots = service.find_free_slots(window_start, window_end, 60)
        assert len(slots) >= 1
        blocked_start = datetime(2026, 9, 3, 13, 0, tzinfo=UTC)
        blocked_end = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)
        for slot in slots:
            overlaps_block = slot.start_utc < blocked_end and slot.end_utc > blocked_start
            assert not overlaps_block

    def test_fully_booked_window_returns_no_slots(self, service: CalendarService) -> None:
        service.create_event(title="All day", start="2026-09-03", duration_days=1)
        window_start, window_end = service.parse_window("2026-09-03", "2026-09-03")
        slots = service.find_free_slots(window_start, window_end, 60)
        assert slots == []

    def test_slots_respect_reference_now(self, service: CalendarService) -> None:
        window_start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
        window_end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
        reference_now = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
        slots = service.find_free_slots(window_start, window_end, 60, now_utc=reference_now)
        assert slots[0].start_utc >= reference_now

    def test_exact_boundary_now_starts_slot_on_the_boundary(self, service: CalendarService) -> None:
        """now exactly on a 5-minute boundary must not be rounded a step up."""
        window_start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
        window_end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
        reference_now = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
        slots = service.find_free_slots(window_start, window_end, 60, now_utc=reference_now)
        assert slots[0].start_utc == reference_now

    def test_sub_boundary_now_rounds_up_to_the_next_boundary(
        self, service: CalendarService
    ) -> None:
        window_start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
        window_end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
        reference_now = datetime(2026, 9, 3, 10, 0, 1, tzinfo=UTC)
        slots = service.find_free_slots(window_start, window_end, 60, now_utc=reference_now)
        assert slots[0].start_utc == datetime(2026, 9, 3, 10, 5, tzinfo=UTC)

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


class TestPersistence:
    def test_events_survive_service_restart(self, tmp_path: Path) -> None:
        service = CalendarService(tmp_path, tz="Europe/Berlin")
        event = service.create_event(
            title="Standup",
            start="2026-08-31T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        reloaded = CalendarService(tmp_path, tz="Europe/Berlin")
        loaded = reloaded.get_event(event.id)
        assert loaded.title == "Standup"
        assert loaded.rrule == event.rrule
        assert loaded.exdates == []

    def test_invalid_entries_are_preserved_and_skipped(self, tmp_path: Path) -> None:
        events_path = tmp_path / "calendar" / "events.json"
        events_path.parent.mkdir(parents=True)
        valid = {
            "id": "valid-1",
            "title": "Valid",
            "all_day": False,
            "start_utc": "2026-09-14T10:00:00+00:00",
            "duration_minutes": 30,
            "created_at": "2026-08-27T00:00:00+00:00",
        }
        events_path.write_text(json.dumps([{"bogus": "entry"}, valid]), encoding="utf-8")
        service = CalendarService(tmp_path)
        assert [event.id for event in service.list_events()] == ["valid-1"]
        # The invalid entry survives saves so no data is silently destroyed.
        other = service.create_event(title="New", start="2026-09-15T10:00:00+00:00")
        raw = json.loads(events_path.read_text(encoding="utf-8"))
        assert {"bogus": "entry"} in [
            entry for entry in raw if isinstance(entry, dict) and "bogus" in entry
        ]
        assert len([item for item in raw if isinstance(item, dict) and item.get("id")]) == 2
        assert other.id

    def test_malformed_storage_degrades_and_blocks_mutations(self, tmp_path: Path) -> None:
        events_path = tmp_path / "calendar" / "events.json"
        events_path.parent.mkdir(parents=True)
        events_path.write_text("{not an array", encoding="utf-8")
        service = CalendarService(tmp_path)
        assert service.list_events() == []
        with pytest.raises(CalendarStorageError):
            service.create_event(title="X", start="2026-09-14")

    def test_changed_callback_fires_on_mutation(self, tmp_path: Path) -> None:
        service = CalendarService(tmp_path)
        calls: list[int] = []
        unsubscribe = service.add_changed_callback(lambda: calls.append(1))
        event = service.create_event(title="X", start="2026-09-14")
        assert calls == [1]
        service.update_event(event.id, title="Y")
        assert calls == [1, 1]
        service.delete_event(event.id)
        assert calls == [1, 1, 1]
        unsubscribe()
        service.create_event(title="Y", start="2026-09-15")
        assert calls == [1, 1, 1]
