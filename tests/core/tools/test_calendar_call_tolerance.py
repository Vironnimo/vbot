"""Calendar calls in other dialects run when their intent is clear, and fail with a call otherwise.

Every reading is checked through production dispatch against the stored state and the text
the Model reads, next to a nearby input that means something else and one that conflicts.
Lengths, time zones and action times: ``test_calendar_call_times.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.core.tools.scheduling_tool_support import (
    DENTIST_START,
    CalendarTool,
    calendar_tool,
    days_of,
    minutes_of,
    start_utc_of,
)


@pytest.fixture
def tool(tmp_path: Path) -> CalendarTool:
    return calendar_tool(tmp_path)


def _refused(text: str) -> bool:
    return text.startswith("Error (invalid_arguments): calendar was not run: ")


class TestActionWords:
    @pytest.mark.parametrize(
        ("word", "expected"),
        [("cancel", "deleted"), ("availability", "free:")],
    )
    def test_action_synonyms_run_the_named_action(
        self, tool: CalendarTool, word: str, expected: str
    ) -> None:
        event_id = tool.add_dentist()
        call: dict[str, Any] = {"action": word, "id": event_id}
        if word == "availability":
            call = {"action": word, "when": "2030-01-10", "duration": 30}

        _, text = tool.call(call)

        assert expected in text

    def test_unknown_action_word_is_refused_with_the_actions(self, tool: CalendarTool) -> None:
        _, text = tool.call({"action": "bogus"})

        assert '"action" must be one of "list", "create"' in text

    def test_call_without_action_is_inferred_from_its_fields(self, tool: CalendarTool) -> None:
        _, created = tool.call({"summary": "Lunch", "start": "2030-01-11T12:00"})
        _, free = tool.call({"duration": 30, "when": "2030-01-11"})
        envelope, _ = tool.call({})

        assert tool.only_event().title == "Lunch"
        assert "title: Lunch" in created
        assert "2030-01-11T13:00 to 2030-01-12T00:00" in free
        assert "events" in envelope["data"]

    def test_bare_id_offers_delete_or_list_and_deletes_nothing(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call({"id": event_id})

        assert len(tool.events()) == 1
        assert text.endswith(
            f'{{"action":"delete","id":"{event_id}"}} or {{"action":"list","id":"{event_id}"}}'
        )

    def test_json_string_arguments_are_read(self, tool: CalendarTool) -> None:
        tool.add_dentist()

        envelope, _ = tool.call(json.dumps({"action": "list", "when": "2030-01"}))

        assert envelope["data"]["events"] == 1


class TestEventFields:
    def test_google_field_names_create_the_event(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "summary": "Review",
                "description": "Bring the draft.",
                "start_time": DENTIST_START,
                "duration_minutes": 30,
            }
        )

        event = tool.only_event()
        assert (event.title, event.description, minutes_of(event)) == (
            "Review",
            "Bring the draft.",
            30,
        )
        assert "end: 2030-01-10T15:30" in text

    def test_equal_title_spellings_merge_but_different_ones_conflict(
        self, tool: CalendarTool
    ) -> None:
        tool.call({"action": "create", "summary": "Same", "title": "Same", "start": DENTIST_START})
        _, conflict = tool.call(
            {"action": "create", "summary": "A", "title": "B", "start": DENTIST_START}
        )

        assert [event.title for event in tool.events()] == ["Same"]
        assert "Conflicting values for title" in conflict

    def test_wrapped_google_event_with_offsets_keeps_its_length(self, tool: CalendarTool) -> None:
        tool.call(
            {
                "event": {
                    "summary": "Wrapped",
                    "start": {"dateTime": "2030-01-10T15:00:00+01:00"},
                    "end": {"dateTime": "2030-01-10T16:15:00+01:00"},
                }
            }
        )

        event = tool.only_event()
        assert (start_utc_of(event), minutes_of(event)) == ("2030-01-10T14:00:00+00:00", 75)

    def test_google_all_day_end_date_is_exclusive(self, tool: CalendarTool) -> None:
        tool.call(
            {"summary": "Trip", "start": {"date": "2030-01-10"}, "end": {"date": "2030-01-12"}}
        )

        event = tool.only_event()
        assert (event.start, days_of(event)) == ("2030-01-10", 2)

    def test_start_and_end_in_different_zones_are_exact_moments(self, tool: CalendarTool) -> None:
        tool.call(
            {
                "summary": "Flight",
                "start": {"dateTime": "2030-01-10T15:00:00", "timeZone": "Europe/Berlin"},
                "end": {"dateTime": "2030-01-10T16:00:00", "timeZone": "America/New_York"},
            }
        )

        event = tool.only_event()
        # 16:00 in New York is 22:00 in Berlin: seven hours after 15:00 Berlin.
        assert (start_utc_of(event), minutes_of(event)) == ("2030-01-10T14:00:00+00:00", 420)

    def test_time_object_without_a_time_is_refused(self, tool: CalendarTool) -> None:
        _, text = tool.call({"summary": "X", "start": {"timeZone": "Europe/Berlin"}})

        assert tool.events() == []
        assert '"start" needs a date or dateTime.' in text

    def test_location_is_kept_apart_from_the_notes(self, tool: CalendarTool) -> None:
        tool.call(
            {
                "action": "create",
                "title": "Talk",
                "start": DENTIST_START,
                "location": "Room 4",
                "notes": "Bring slides.",
            }
        )
        event_id = tool.only_event().id
        tool.call({"action": "update", "id": event_id, "location": "Hall B"})

        event = tool.only_event()
        assert (event.location, event.description) == ("Hall B", "Bring slides.")

    def test_attendees_are_refused_with_a_call_that_records_them(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "Sync",
                "start": DENTIST_START,
                "attendees": [{"email": "a@example.org"}, "Sam"],
            }
        )

        assert tool.events() == []
        assert text.endswith(
            'Send: {"action":"create","title":"Sync","start":"2030-01-10T15:00",'
            '"notes":"Attendees: a@example.org, Sam"}'
        )

    def test_requested_reminders_are_refused_but_default_ones_are_dropped(
        self, tool: CalendarTool
    ) -> None:
        _, refused = tool.call(
            {
                "action": "create",
                "title": "A",
                "start": DENTIST_START,
                "reminders": {"useDefault": False, "overrides": [{"minutes": 10}]},
            }
        )
        tool.call(
            {
                "action": "create",
                "title": "B",
                "start": DENTIST_START,
                "reminders": {"useDefault": True},
            }
        )

        assert [event.title for event in tool.events()] == ["B"]
        assert "the calendar has no reminders. After creating the event, add_action" in refused

    def test_primary_calendar_is_accepted_and_another_is_refused(self, tool: CalendarTool) -> None:
        tool.call(
            {"action": "create", "title": "A", "start": DENTIST_START, "calendarId": "primary"}
        )
        _, refused = tool.call(
            {"action": "create", "title": "B", "start": DENTIST_START, "calendarId": "work"}
        )

        assert [event.title for event in tool.events()] == ["A"]
        assert 'there is one local calendar; "calendarId" "work" cannot select another.' in refused


class TestRepetition:
    @pytest.mark.parametrize(
        ("rule", "stored"),
        [
            ("FREQ=WEEKLY;BYDAY=MO,WE", "FREQ=WEEKLY;BYDAY=MO,WE"),
            (["RRULE:FREQ=DAILY;COUNT=5"], "FREQ=DAILY;COUNT=5"),
            ({"freq": "weekly", "interval": 2}, "FREQ=WEEKLY;INTERVAL=2"),
            ("every 2 weeks", "FREQ=WEEKLY;INTERVAL=2"),
            ("weekdays", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
            ("FREQ=MONTHLY;BYMONTHDAY=10", "FREQ=MONTHLY"),
        ],
    )
    def test_rule_texts_become_the_stored_rule(
        self, tool: CalendarTool, rule: Any, stored: str
    ) -> None:
        tool.call({"action": "create", "title": "R", "start": DENTIST_START, "rrule": rule})

        assert tool.only_event().rrule == stored

    @pytest.mark.parametrize(
        ("rule", "reason"),
        [
            ("FREQ=DAILY;INTERVAL=abc", "rrule INTERVAL must be a whole number from 1 to 1000"),
            (
                {"freq": "daily", "count": "\u00b3"},
                "rrule COUNT must be a whole number from 1 to 10000",
            ),
        ],
    )
    def test_rule_number_that_is_no_whole_number_is_refused(
        self, tool: CalendarTool, rule: Any, reason: str
    ) -> None:
        _, text = tool.call(
            {"action": "create", "title": "R", "start": DENTIST_START, "rrule": rule}
        )

        assert _refused(text)
        assert reason in text
        assert tool.events() == []

    def test_top_level_rule_parts_are_folded_into_rrule(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "R",
                "start": DENTIST_START,
                "freq": "daily",
                "interval": 2,
            }
        )

        assert "repeats: FREQ=DAILY;INTERVAL=2" in text

    def test_rule_part_on_another_day_is_refused_with_the_keepable_rule(
        self, tool: CalendarTool
    ) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "R",
                "start": DENTIST_START,
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=15",
            }
        )

        assert tool.events() == []
        assert "the calendar cannot repeat by BYMONTHDAY" in text
        assert text.endswith('"rrule":{"freq":"monthly"}}')

    def test_rule_given_twice_is_refused(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "R",
                "start": DENTIST_START,
                "rrule": {"freq": "weekly"},
                "freq": "daily",
            }
        )

        assert tool.events() == []
        assert "repetition is given twice (rrule and top-level fields); send rrule." in text


class TestIds:
    def test_missing_id_is_named_from_a_matching_title(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, deleted = tool.call({"action": "delete", "title": "dentist"})
        _, updated = tool.call(
            {"action": "update", "title": "Dentist", "start": "2030-01-10T16:00"}
        )

        assert len(tool.events()) == 1
        assert deleted.endswith(f'Send: {{"action":"delete","id":"{event_id}"}}')
        assert f'"id":"{event_id}","title":"Dentist","start":"2030-01-10T16:00"' in updated

    def test_missing_id_with_several_matching_titles_offers_each(self, tool: CalendarTool) -> None:
        first = tool.service.create_event(title="Gym", start="2030-01-10T08:00")
        second = tool.service.create_event(title="Gym", start="2030-01-12T08:00")

        _, text = tool.call({"action": "delete", "title": "Gym"})

        assert len(tool.events()) == 2
        assert f'{{"action":"delete","id":"{first.id}"}} ("Gym" at 2030-01-10T08:00) or ' in text
        assert f'{{"action":"delete","id":"{second.id}"}} ("Gym" at 2030-01-12T08:00)' in text

    def test_missing_id_without_a_match_points_to_list(self, tool: CalendarTool) -> None:
        tool.add_dentist()

        _, text = tool.call({"action": "delete", "title": "Nothing"})

        assert len(tool.events()) == 1
        assert text.endswith('Send: {"action":"delete","id":"<event id from list>"}')

    def test_placeholder_id_counts_as_missing(self, tool: CalendarTool) -> None:
        tool.add_dentist()

        _, text = tool.call({"action": "update", "id": "<event id>", "notes": "x"})

        assert tool.only_event().description is None
        assert 'update needs the event "id"' in text


class TestStandIns:
    @pytest.mark.parametrize(
        ("field", "stand_in"), [("title", "<title>"), ("notes", "<the notes from this call>")]
    )
    def test_stand_in_event_text_is_refused_on_create_and_update(
        self, tool: CalendarTool, field: str, stand_in: str
    ) -> None:
        event_id = tool.add_dentist()

        _, created = tool.call(
            {"action": "create", "title": "T", "start": DENTIST_START, field: stand_in}
        )
        _, updated = tool.call({"action": "update", "id": event_id, field: stand_in})

        event = tool.only_event()
        assert (event.title, event.description) == ("Dentist", None)
        assert f'{field} "{stand_in}" is a stand-in.' in created
        assert f'{field} "{stand_in}" is a stand-in.' in updated


class TestImpossibleValues:
    @pytest.mark.parametrize(
        ("call", "reason"),
        [
            (
                {"action": "create", "title": "E", "start": DENTIST_START, "end": "25:99"},
                '"end" 25:99 must be a local time',
            ),
            (
                {"action": "create", "title": "E", "start": "2030-01-10", "end": "16:00"},
                "an all-day event ends on a date; send its length in days as duration.",
            ),
            (
                {"action": "create", "title": "E", "start": "2030-01-10", "end": "2030-02-30"},
                '"end" 2030-02-30 does not exist: February 2030 has 28 days. For an all-day '
                "event, send its length in days as duration.",
            ),
            (
                {"action": "create", "title": "E", "start": "2030-01-10Z"},
                "start must be a valid ISO 8601 datetime",
            ),
        ],
        ids=[
            "end-time",
            "all-day-end-time",
            "all-day-end-missing-date",
            "date-with-utc-marker",
        ],
    )
    def test_value_naming_no_real_time_is_refused(
        self, tool: CalendarTool, call: dict[str, Any], reason: str
    ) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call(call)

        assert _refused(text)
        assert reason in text
        assert [event.id for event in tool.events()] == [event_id]


class TestConflicts:
    def test_create_with_an_id_offers_update_or_create(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call(
            {"action": "create", "id": event_id, "title": "New", "start": DENTIST_START}
        )

        assert [event.title for event in tool.events()] == ["Dentist"]
        assert f'{{"action":"update","id":"{event_id}","title":"New"' in text
        assert '{"action":"create","title":"New","start":"2030-01-10T15:00"}' in text

    def test_delete_with_changes_offers_delete_or_update(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call({"action": "delete", "id": event_id, "title": "Renamed"})

        assert [event.title for event in tool.events()] == ["Dentist"]
        assert text.endswith(
            f'{{"action":"delete","id":"{event_id}"}} or '
            f'{{"action":"update","id":"{event_id}","title":"Renamed"}}'
        )

    def test_list_with_a_title_searches_for_it(self, tool: CalendarTool) -> None:
        tool.add_dentist()
        tool.service.create_event(title="Gym", start="2030-01-10T08:00")

        _, text = tool.call({"action": "list", "title": "dent", "start": "2030-01-10T09:00"})

        assert "window: 2030-01-10" in text
        assert "title: Dentist" in text
        assert "Gym" not in text

    def test_list_describing_an_event_offers_create(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {"action": "list", "title": "Party", "start": "2030-01-10T20:00", "notes": "Cake."}
        )

        assert tool.events() == []
        assert text.endswith(
            '{"action":"create","title":"Party","start":"2030-01-10T20:00","notes":"Cake."}'
        )

    def test_list_with_an_instruction_is_refused_with_the_read(self, tool: CalendarTool) -> None:
        _, text = tool.call({"action": "list", "when": "2030-01", "prompt": "Summarize."})

        assert "list only reads the calendar; prompt would change it." in text
        assert text.endswith('Send: {"action":"list","when":"2030-01"}')

    def test_window_given_twice_offers_both(self, tool: CalendarTool) -> None:
        _, text = tool.call({"action": "list", "when": "2030-01", "start": "2030-01-10"})

        assert text.endswith(
            '{"action":"list","when":"2030-01"} or {"action":"list","when":"2030-01-10"}'
        )

    def test_start_given_twice_offers_both(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "W",
                "start": DENTIST_START,
                "when": "2030-01-11T10:00",
            }
        )

        assert tool.events() == []
        assert '"start":"2030-01-10T15:00"} or ' in text
        assert '"start":"2030-01-11T10:00"}' in text

    def test_create_with_action_fields_names_the_create_call(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "X",
                "start": DENTIST_START,
                "when": "start - 1h",
                "prompt": "Remind me.",
            }
        )

        assert tool.events() == []
        assert "when, prompt belong to an action, which attaches to an existing event." in text
        assert text.endswith('{"action":"create","title":"X","start":"2030-01-10T15:00"}')
