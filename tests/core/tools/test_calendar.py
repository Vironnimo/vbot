"""Behavior of the calendar Tool through production dispatch: state and the text the Model reads."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.tools.calendar import (
    CALENDAR_TOOL_NAME,
    CALENDAR_TOOL_PARAMETERS,
)
from tests.core.tools.scheduling_tool_support import WEEKLY_MONDAY, calendar_tool

BERLIN = ZoneInfo("Europe/Berlin")


def test_definition_advertises_the_calendar_parameters() -> None:
    assert set(CALENDAR_TOOL_PARAMETERS["properties"]) == {
        "action",
        "when",
        "id",
        "title",
        "start",
        "duration",
        "rrule",
        "notes",
    }


class TestList:
    def test_list_defaults_to_the_current_month(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        window_start, window_end = tool.service.resolve_when("this month")
        inside = (window_start.astimezone(BERLIN) + timedelta(days=2)).date().isoformat()
        outside = (window_end.astimezone(BERLIN) + timedelta(days=10)).date().isoformat()
        tool.service.create_event(title="In month", start=f"{inside}T10:00:00")
        tool.service.create_event(title="Far away", start=f"{outside}T10:00:00")

        envelope, text = tool.call({"action": "list"})

        assert envelope["data"]["events"] == 1
        assert envelope["data"]["occurrences"] == 1
        assert "title: In month" in text
        assert "Far away" not in text
        assert "timezone: Europe/Berlin" in text

    def test_list_shows_events_with_ids_times_repetition_and_notes(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        weekly = tool.service.create_event(
            title="Weekly", start="2030-01-07T09:00:00", end="2030-01-07T09:30", rrule=WEEKLY_MONDAY
        )
        trip = tool.service.create_event(
            title="Trip",
            start="2030-01-08",
            end="2030-01-11",
            location="Lake",
            description="Pack.\nBook seats.",
        )

        envelope, text = tool.call({"action": "list", "when": "2030-01-06..2030-01-19"})

        assert envelope["data"]["occurrences"] == 3
        assert text == (
            "events: 2\n"
            "occurrences: 3\n"
            "window: 2030-01-06 to 2030-01-19\n"
            "timezone: Europe/Berlin\n"
            "\n"
            f"id: {weekly.id}\n"
            "title: Weekly\n"
            "start: 2030-01-07T09:00\n"
            "end: 2030-01-07T09:30\n"
            "repeats: FREQ=WEEKLY;BYDAY=MO\n"
            "occurrences: 2030-01-07T09:00, 2030-01-14T09:00\n"
            "\n"
            f"id: {trip.id}\n"
            "title: Trip\n"
            "start: 2030-01-08\n"
            "days: 3\n"
            "location: Lake\n"
            "notes: Pack.\n"
            "  Book seats."
        )

    def test_list_abbreviates_many_occurrences(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        tool.service.create_event(title="Daily", start="2030-01-01T08:00", rrule="FREQ=DAILY")

        _, text = tool.call({"action": "list", "when": "2030-01"})

        assert (
            "occurrences: 31 in this window, 2030-01-01T08:00, 2030-01-02T08:00, "
            "2030-01-03T08:00, ..., 2030-01-31T08:00"
        ) in text

    def test_list_by_id_shows_the_event_even_outside_the_window(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="Later", start="2030-03-01T10:00")
        tool.service.create_event(title="Other", start="2030-01-05T10:00")

        envelope, text = tool.call({"action": "list", "id": event.id, "when": "2030-01"})

        assert envelope["data"]["events"] == 1
        assert envelope["data"]["occurrences"] == 0
        assert f"id: {event.id}" in text
        assert "Other" not in text

    def test_list_filters_by_query_in_title_and_notes(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        tool.service.create_event(title="Dentist", start="2030-01-05T10:00")
        tool.service.create_event(
            title="Call", start="2030-01-06T10:00", description="about the DENTIST"
        )
        tool.service.create_event(title="Gym", start="2030-01-07T10:00")

        envelope, text = tool.call({"action": "list", "when": "2030-01", "query": "dentist"})

        assert envelope["data"]["events"] == 2
        assert "matching: dentist" in text
        assert "Gym" not in text

    def test_list_rejects_unknown_when_with_a_corrected_call(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        envelope, text = tool.call({"action": "list", "when": "someday"})

        assert envelope["error"]["code"] == "invalid_arguments"
        assert "cannot parse when 'someday'" in text
        assert text.endswith('Send: {"action":"list","when":"this week"}')


class TestCreate:
    def test_create_timed_event_with_default_length(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        _, text = tool.call({"action": "create", "title": "Dentist", "start": "2030-01-10T15:00"})

        event = tool.only_event()
        assert (event.start, event.end, event.tz_name) == (
            "2030-01-10T15:00:00",
            "2030-01-10T16:00:00",
            "Europe/Berlin",
        )
        assert text == (
            f"id: {event.id}\ntitle: Dentist\nstart: 2030-01-10T15:00\nend: 2030-01-10T16:00"
        )

    def test_create_all_day_event_counts_days(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        _, text = tool.call(
            {"action": "create", "title": "Trip", "start": "2030-01-14", "duration": 3}
        )

        event = tool.only_event()
        assert (event.all_day, event.start, event.end) == (True, "2030-01-14", "2030-01-17")
        assert "start: 2030-01-14\ndays: 3" in text

    def test_create_repeating_event_anchors_in_server_zone(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        _, text = tool.call(
            {
                "action": "create",
                "title": "Standup",
                "start": "2030-01-07T09:00:00",
                "rrule": WEEKLY_MONDAY,
            }
        )

        event = tool.only_event()
        assert (event.tz_name, event.start) == ("Europe/Berlin", "2030-01-07T09:00:00")
        assert "repeats: FREQ=WEEKLY;BYDAY=MO" in text

    def test_create_without_title_names_the_call_with_its_start(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        _, text = tool.call({"action": "create", "start": "2030-01-10T15:00"})

        assert tool.events() == []
        assert text == (
            'Error (invalid_arguments): calendar was not run: create needs "title". Send: '
            '{"action":"create","title":"<title>","start":"2030-01-10T15:00"}'
        )

    def test_create_with_null_rrule_names_the_single_event_call(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        _, text = tool.call(
            {"action": "create", "title": "X", "start": "2030-01-10T15:00", "rrule": None}
        )

        assert tool.events() == []
        assert text.endswith('Send: {"action":"create","title":"X","start":"2030-01-10T15:00"}')


class TestUpdate:
    def test_update_changes_only_sent_fields(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(
            title="Standup", start="2030-01-07T09:00:00", rrule=WEEKLY_MONDAY
        )

        _, text = tool.call({"action": "update", "id": event.id, "title": "Daily"})

        updated = tool.only_event()
        assert (updated.title, updated.end) == ("Daily", "2030-01-07T10:00:00")
        assert updated.rrule is not None
        assert "title: Daily" in text

    def test_update_duration_follows_the_event_kind(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        timed = tool.service.create_event(title="X", start="2030-01-10T15:00:00")
        trip = tool.service.create_event(title="Trip", start="2030-01-14")

        tool.call({"action": "update", "id": timed.id, "duration": 90})
        tool.call({"action": "update", "id": trip.id, "duration": 5})

        assert tool.service.get_event(timed.id).end == "2030-01-10T16:30:00"
        assert tool.service.get_event(trip.id).end == "2030-01-19"

    def test_update_start_switches_all_day_event_to_timed(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="Trip", start="2030-01-14", end="2030-01-17")

        tool.call({"action": "update", "id": event.id, "start": "2030-01-14T15:00", "duration": 60})

        updated = tool.only_event()
        assert (updated.all_day, updated.start, updated.end) == (
            False,
            "2030-01-14T15:00:00",
            "2030-01-14T16:00:00",
        )

    def test_update_null_rrule_stops_repetition(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(
            title="Standup", start="2030-01-07T09:00:00", rrule=WEEKLY_MONDAY
        )

        _, text = tool.call({"action": "update", "id": event.id, "rrule": None})

        updated = tool.only_event()
        assert (updated.rrule, updated.start) == (None, "2030-01-07T09:00:00")
        assert "repeats" not in text

    def test_update_without_changes_names_a_call(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="X", start="2030-01-10T15:00:00")

        _, text = tool.call({"action": "update", "id": event.id})

        assert "update needs a field to change: title, start, duration, rrule or notes." in text
        assert f'"id":"{event.id}","start":"<2030-01-10 or 2030-01-10T15:00>"' in text

    def test_update_of_unknown_id_points_to_list(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        envelope, text = tool.call({"action": "update", "id": "evt_missing", "title": "X"})

        assert envelope["error"]["code"] == "event_not_found"
        assert text == (
            'Error (event_not_found): No event has id "evt_missing". {"action":"list"} shows '
            'events and their ids; add a when such as "next month" to look further ahead.'
        )


class TestDelete:
    def test_delete_removes_the_event(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="X", start="2030-01-10T15:00:00")

        _, text = tool.call({"action": "delete", "id": event.id})

        assert tool.events() == []
        assert text == f"id: {event.id}\ntitle: X\nstatus: deleted"

    def test_delete_with_occurrence_start_removes_one_occurrence(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(
            title="Standup", start="2030-01-07T09:00:00", rrule=WEEKLY_MONDAY
        )

        _, text = tool.call({"action": "delete", "id": event.id, "start": "2030-01-14T09:00"})

        assert tool.only_event().exdates == ["2030-01-14T09:00:00"]
        assert "removed_occurrence: 2030-01-14T09:00" in text
        _, listed = tool.call({"action": "list", "when": "2030-01-06..2030-01-21"})
        assert "occurrences: 2030-01-07T09:00, 2030-01-21T09:00" in listed
        assert "removed_occurrences: 2030-01-14T09:00" in listed

    def test_delete_with_a_start_that_is_no_occurrence_offers_nearby_ones(
        self, tmp_path: Path
    ) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(
            title="Standup", start="2030-01-07T09:00:00", rrule=WEEKLY_MONDAY
        )

        _, text = tool.call({"action": "delete", "id": event.id, "start": "2030-01-15T09:00"})

        assert tool.only_event().exdates == []
        assert text == (
            'Error (invalid_arguments): calendar was not run: "2030-01-15T09:00" is not an '
            "occurrence of this event. Nearby occurrences: "
            f'{{"action":"delete","id":"{event.id}","start":"2030-01-14T09:00"}} or '
            f'{{"action":"delete","id":"{event.id}","start":"2030-01-21T09:00"}}'
        )

    def test_delete_of_a_single_event_at_its_start_deletes_it(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="X", start="2030-01-10T15:00:00")

        _, text = tool.call({"action": "delete", "id": event.id, "start": "2030-01-10T15:00"})

        assert tool.events() == []
        assert "note: The event does not repeat, so the whole event was deleted." in text

    def test_delete_of_a_single_event_at_another_start_is_refused(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="X", start="2030-01-10T15:00:00")

        _, text = tool.call({"action": "delete", "id": event.id, "start": "2030-01-11T15:00"})

        assert len(tool.events()) == 1
        assert "the event does not repeat and starts at 2030-01-10T15:00" in text
        assert text.endswith(f'Send: {{"action":"delete","id":"{event.id}"}}')


class TestFindFree:
    def test_find_free_shows_whole_free_spans_around_events(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        tool.service.create_event(title="Block", start="2030-09-03T15:00:00+02:00")

        envelope, text = tool.call({"action": "find_free", "when": "2030-09-03", "duration": 60})

        assert envelope["data"]["free"] == 2
        assert text == (
            "free: 2\n"
            "window: 2030-09-03\n"
            "timezone: Europe/Berlin\n"
            "\n"
            "2030-09-03T00:00 to 2030-09-03T15:00 (15h)\n"
            "2030-09-03T16:00 to 2030-09-04T00:00 (8h)"
        )

    def test_find_free_skips_gaps_shorter_than_the_duration(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        tool.service.create_event(title="A", start="2030-09-03T09:00", end="2030-09-03T10:00")
        tool.service.create_event(title="B", start="2030-09-03T10:30", end="2030-09-03T11:30")

        _, text = tool.call({"action": "find_free", "when": "2030-09-03", "duration": 45})

        assert "2030-09-03T10:00 to 2030-09-03T10:30" not in text
        assert "2030-09-03T11:30 to 2030-09-04T00:00 (12h 30m)" in text

    def test_find_free_notes_more_free_time_beyond_ten_spans(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        for day in range(1, 13):
            tool.service.create_event(title="Busy", start=f"2030-09-{day:02d}T12:00")

        envelope, text = tool.call(
            {"action": "find_free", "when": "2030-09-01..2030-09-12", "duration": 60}
        )

        assert envelope["data"]["free"] == 10
        assert "note: More free time follows after 2030-09-10T12:00; a later when shows it." in text

    def test_find_free_defaults_to_the_next_seven_days(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        today, _ = tool.service.resolve_when("today")

        envelope, text = tool.call({"action": "find_free"})

        first_day = today.astimezone(BERLIN).date()
        last_day = first_day + timedelta(days=6)
        assert f"window: {first_day.isoformat()}" in text
        assert envelope["data"]["free"] == 1
        assert f"to {(last_day + timedelta(days=1)).isoformat()}T00:00" in text

    def test_find_free_rejects_zero_duration(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        envelope, _ = tool.call({"action": "find_free", "when": "this week", "duration": 0})

        assert envelope["error"]["code"] == "invalid_arguments"


def test_display_labels_the_meant_action_of_a_dialect_call(tmp_path: Path) -> None:
    tool = calendar_tool(tmp_path)

    display = tool.registry.display_for_call(
        CALENDAR_TOOL_NAME, {"summary": "Dentist", "start": "2030-01-10T15:00"}
    )

    assert [part["value"] for part in display["primary"]] == ["create", "Dentist"]
