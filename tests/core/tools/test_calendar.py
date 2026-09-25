"""Behavior of the calendar Tool through production dispatch: state and the text the Model reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from core.tools.calendar import CALENDAR_TOOL_DESCRIPTION, CALENDAR_TOOL_PARAMETERS
from tests.core.tools.calendar_tool_support import calendar_tool

BERLIN = ZoneInfo("Europe/Berlin")
WEEKLY_MONDAY = {"freq": "weekly", "by_weekday": ["mo"]}


def test_definition_names_actions_time_zone_and_the_cron_alternative() -> None:
    assert "Time zone shown in Runtime Environment" in CALENDAR_TOOL_DESCRIPTION
    assert "use cron if available" in CALENDAR_TOOL_DESCRIPTION
    assert set(CALENDAR_TOOL_PARAMETERS["properties"]) == {
        "action",
        "when",
        "id",
        "title",
        "start",
        "duration",
        "rrule",
        "notes",
        "prompt",
        "target",
        "session",
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
            title="Weekly", start="2030-01-07T09:00:00", duration_minutes=30, rrule=WEEKLY_MONDAY
        )
        trip = tool.service.create_event(
            title="Trip", start="2030-01-08", duration_days=3, notes="Pack.\nBook seats."
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
            'repeats: {"freq":"weekly","by_weekday":["mo"]}\n'
            "occurrences: 2030-01-07T09:00, 2030-01-14T09:00\n"
            "\n"
            f"id: {trip.id}\n"
            "title: Trip\n"
            "start: 2030-01-08\n"
            "days: 3\n"
            "notes: Pack.\n"
            "  Book seats."
        )

    def test_list_abbreviates_many_occurrences(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        tool.service.create_event(title="Daily", start="2030-01-01T08:00", rrule={"freq": "daily"})

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
        tool.service.create_event(title="Call", start="2030-01-06T10:00", notes="about the DENTIST")
        tool.service.create_event(title="Gym", start="2030-01-07T10:00")

        envelope, text = tool.call({"action": "list", "when": "2030-01", "query": "dentist"})

        assert envelope["data"]["events"] == 2
        assert "matching: dentist" in text
        assert "Gym" not in text

    def test_list_shows_actions_with_next_due_time(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="Meeting", start="2030-01-10T12:00")
        action = tool.service.actions.add(
            event.id, when="start - 1h", prompt="Prepare the notes.", target="agent-one"
        )

        _, text = tool.call({"action": "list", "when": "2030-01"})

        assert text.endswith(
            f"action {action['id']}: start - 1h, runs agent-one in a fresh Session\n"
            "  prompt: Prepare the notes.\n"
            "  next: 2030-01-10T11:00"
        )

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
        assert event.start_utc == "2030-01-10T14:00:00+00:00"
        assert event.duration_minutes == 60
        assert text == (
            f"id: {event.id}\ntitle: Dentist\nstart: 2030-01-10T15:00\nend: 2030-01-10T16:00"
        )

    def test_create_all_day_event_counts_days(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        _, text = tool.call(
            {"action": "create", "title": "Trip", "start": "2030-01-14", "duration": 3}
        )

        event = tool.only_event()
        assert (event.all_day, event.duration_days, event.duration_minutes) == (True, 3, None)
        assert "start: 2030-01-14\ndays: 3" in text

    def test_single_event_end_uses_real_time_across_dst_fall_back(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)

        _, created = tool.call(
            {
                "action": "create",
                "title": "Night shift",
                "start": "2030-10-27T01:30:00",
                "duration": 120,
            }
        )
        _, free = tool.call({"action": "find_free", "when": "2030-10-27", "duration": 60})

        # 01:30 CEST plus two real hours is 02:30 CET, not 03:30 wall-clock time.
        assert "end: 2030-10-27T02:30" in created
        assert "2030-10-27T02:30 to 2030-10-28T00:00" in free

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
        assert (event.tz_name, event.start_local) == ("Europe/Berlin", "2030-01-07T09:00:00")
        assert 'repeats: {"freq":"weekly","by_weekday":["mo"]}' in text

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
        assert (updated.title, updated.duration_minutes) == ("Daily", 60)
        assert updated.rrule is not None
        assert "title: Daily" in text

    def test_update_duration_follows_the_event_kind(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        timed = tool.service.create_event(title="X", start="2030-01-10T15:00:00")
        trip = tool.service.create_event(title="Trip", start="2030-01-14")

        tool.call({"action": "update", "id": timed.id, "duration": 90})
        tool.call({"action": "update", "id": trip.id, "duration": 5})

        assert tool.service.get_event(timed.id).duration_minutes == 90
        assert tool.service.get_event(trip.id).duration_days == 5

    def test_update_start_switches_all_day_event_to_timed(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="Trip", start="2030-01-14", duration_days=3)

        tool.call({"action": "update", "id": event.id, "start": "2030-01-14T15:00", "duration": 60})

        updated = tool.only_event()
        assert (updated.all_day, updated.start_utc) == (False, "2030-01-14T14:00:00+00:00")
        assert (updated.duration_minutes, updated.duration_days) == (60, None)

    def test_update_null_rrule_stops_repetition(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(
            title="Standup", start="2030-01-07T09:00:00", rrule=WEEKLY_MONDAY
        )

        _, text = tool.call({"action": "update", "id": event.id, "rrule": None})

        updated = tool.only_event()
        assert (updated.rrule, updated.start_utc) == (None, "2030-01-07T08:00:00+00:00")
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
            'events, their actions and ids; add a when such as "next month" to look further '
            "ahead."
        )


class TestDelete:
    def test_delete_removes_the_event_and_its_actions(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="X", start="2030-01-10T15:00:00")
        tool.service.actions.add(event.id, when="start", prompt="p", target="agent-one")

        _, text = tool.call({"action": "delete", "id": event.id})

        assert tool.events() == []
        assert tool.actions() == []
        assert text == f"id: {event.id}\ntitle: X\nstatus: deleted\nactions_removed: 1"

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
        tool.service.create_event(title="A", start="2030-09-03T09:00", duration_minutes=60)
        tool.service.create_event(title="B", start="2030-09-03T10:30", duration_minutes=60)

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


class TestActions:
    def test_actions_default_to_current_agent_and_fresh_session(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="Meeting", start="2030-01-01T12:00")

        _, added = tool.call(
            {"action": "add_action", "id": event.id, "when": "start - 1h", "prompt": "prepare"}
        )

        [action] = tool.actions()
        assert (action["target"], action["session"], action["when"]) == (
            "agent-one",
            None,
            "start - 1h",
        )
        assert added == (
            f"id: {action['id']}\n"
            f"event: Meeting ({event.id})\n"
            "when: start - 1h\n"
            "target: agent-one\n"
            "session: a fresh Session each time\n"
            "next_due: 2030-01-01T11:00"
        )

        _, changed = tool.call({"action": "update_action", "id": action["id"], "when": "end + 30m"})
        assert tool.actions()[0]["when"] == "end + 30m"
        assert tool.actions()[0]["prompt"] == "prepare"
        assert "next_due: 2030-01-01T13:30" in changed

        _, deleted = tool.call({"action": "delete_action", "id": action["id"]})
        assert tool.actions() == []
        assert deleted == f"id: {action['id']}\nevent: Meeting ({event.id})\nstatus: deleted"

    def test_explicit_target_and_session_are_kept(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="Meeting", start="2030-01-01T12:00")

        tool.call(
            {
                "action": "add_action",
                "id": event.id,
                "when": "start",
                "prompt": "prepare",
                "target": "builder@project",
                "session": "chosen",
            }
        )

        [action] = tool.actions()
        assert (action["target"], action["session"]) == ("builder@project", "chosen")

    def test_unknown_field_is_refused_before_any_action_exists(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(title="Meeting", start="2030-01-01T12:00")

        _, text = tool.call(
            {
                "action": "add_action",
                "id": event.id,
                "when": "start",
                "prompt": "p",
                "catch_up_minutes": 60,
            }
        )

        assert tool.actions() == []
        assert '"catch_up_minutes" is not a parameter.' in text

    def test_action_on_a_past_event_says_it_will_not_run(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        past = (datetime.now(UTC) - timedelta(days=90)).astimezone(BERLIN)
        event = tool.service.create_event(
            title="Old", start=past.replace(tzinfo=None, microsecond=0).isoformat()
        )

        _, text = tool.call(
            {"action": "add_action", "id": event.id, "when": "start", "prompt": "p"}
        )

        assert len(tool.actions()) == 1
        assert "note: No occurrence of the event lies ahead, so the action will not run." in text
        assert "next_due" not in text

    def test_repeating_event_action_names_its_next_due_time(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path)
        event = tool.service.create_event(
            title="Standup", start="2030-01-07T09:00", rrule=WEEKLY_MONDAY
        )

        _, text = tool.call(
            {"action": "add_action", "id": event.id, "when": "start - 15m", "prompt": "p"}
        )

        assert "next_due: 2030-01-07T08:45" in text


def test_display_labels_the_meant_action_of_a_dialect_call(tmp_path: Path) -> None:
    tool = calendar_tool(tmp_path)
    [registered] = tool.registry.list_tools()
    assert registered.display is not None
    parts_builder = registered.display.parts_builder
    assert parts_builder is not None

    parts = parts_builder({"summary": "Dentist", "start": "2030-01-10T15:00"})

    assert [part.value for part in parts] == ["create", "Dentist"]
