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

from tests.core.tools.scheduling_tool_support import DENTIST_START, CalendarTool, calendar_tool


@pytest.fixture
def tool(tmp_path: Path) -> CalendarTool:
    return calendar_tool(tmp_path)


def _refused(text: str) -> bool:
    return text.startswith("Error (invalid_arguments): calendar was not run: ")


class TestActionWords:
    @pytest.mark.parametrize(
        ("word", "expected"),
        [("cancel", "deleted"), ("availability", "free:"), ("add_reminder", "next_due:")],
    )
    def test_action_synonyms_run_the_named_action(
        self, tool: CalendarTool, word: str, expected: str
    ) -> None:
        event_id = tool.add_dentist()
        call: dict[str, Any] = {"action": word, "id": event_id}
        if word == "availability":
            call = {"action": word, "when": "2030-01-10", "duration": 30}
        if word == "add_reminder":
            call.update(when="start - 1h", prompt="Remind me.")

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
        assert (event.title, event.notes, event.duration_minutes) == (
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
        assert (event.start_utc, event.duration_minutes) == ("2030-01-10T14:00:00+00:00", 75)

    def test_google_all_day_end_date_is_exclusive(self, tool: CalendarTool) -> None:
        tool.call(
            {"summary": "Trip", "start": {"date": "2030-01-10"}, "end": {"date": "2030-01-12"}}
        )

        event = tool.only_event()
        assert (event.start_date, event.duration_days) == ("2030-01-10", 2)

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
        assert (event.start_utc, event.duration_minutes) == ("2030-01-10T14:00:00+00:00", 420)

    def test_time_object_without_a_time_is_refused(self, tool: CalendarTool) -> None:
        _, text = tool.call({"summary": "X", "start": {"timeZone": "Europe/Berlin"}})

        assert tool.events() == []
        assert '"start" needs a date or dateTime.' in text

    def test_location_leads_the_notes_and_replaces_an_earlier_one(self, tool: CalendarTool) -> None:
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

        assert tool.only_event().notes == "Location: Hall B\nBring slides."

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
            ("FREQ=WEEKLY;BYDAY=MO,WE", {"freq": "weekly", "by_weekday": ["mo", "we"]}),
            (["RRULE:FREQ=DAILY;COUNT=5"], {"freq": "daily", "count": 5}),
            ("every 2 weeks", {"freq": "weekly", "interval": 2}),
            ("weekdays", {"freq": "weekly", "by_weekday": ["mo", "tu", "we", "th", "fr"]}),
            ("FREQ=MONTHLY;BYMONTHDAY=10", {"freq": "monthly"}),
        ],
    )
    def test_rule_texts_become_the_rule_object(
        self, tool: CalendarTool, rule: Any, stored: dict[str, Any]
    ) -> None:
        tool.call({"action": "create", "title": "R", "start": DENTIST_START, "rrule": rule})

        event_rule = tool.only_event().rrule
        assert event_rule is not None
        kept = {key: value for key, value in event_rule.items() if value not in (None, 1)}
        if "by_weekday" in kept:
            kept["by_weekday"] = sorted(kept["by_weekday"])
            stored = {**stored, "by_weekday": sorted(stored["by_weekday"])}
        assert kept == stored

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

        assert 'repeats: {"freq":"daily","interval":2}' in text

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
    def test_delete_and_update_of_an_action_id_act_on_the_action(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()
        first = tool.service.actions.add(event_id, when="start", prompt="a", target="agent-one")
        second = tool.service.actions.add(event_id, when="end", prompt="b", target="agent-one")

        tool.call({"action": "update", "id": first["id"], "prompt": "changed"})
        tool.call({"action": "delete", "id": second["id"]})

        assert [(action["id"], action["prompt"]) for action in tool.actions()] == [
            (first["id"], "changed")
        ]
        assert len(tool.events()) == 1

    def test_action_call_with_the_event_id_names_its_one_action(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()
        action = tool.service.actions.add(event_id, when="start", prompt="a", target="agent-one")

        _, text = tool.call({"action": "update_action", "id": event_id, "when": "end"})

        assert tool.actions()[0]["when"] == "start"
        assert text.endswith(
            f'Send: {{"action":"update_action","id":"{action["id"]}","when":"end"}}'
        )

    def test_action_call_with_the_event_id_lists_several_actions(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()
        first = tool.service.actions.add(event_id, when="start", prompt="a", target="agent-one")
        second = tool.service.actions.add(event_id, when="end", prompt="b", target="agent-one")

        _, text = tool.call({"action": "delete_action", "id": event_id})

        assert len(tool.actions()) == 2
        assert f'{{"action":"delete_action","id":"{first["id"]}"}} (start: a) or ' in text
        assert f'{{"action":"delete_action","id":"{second["id"]}"}} (end: b)' in text

    def test_action_call_on_an_event_without_actions_offers_add_action(
        self, tool: CalendarTool
    ) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call({"action": "update_action", "id": event_id, "prompt": "p"})

        assert tool.actions() == []
        assert text.endswith(
            f'{{"action":"add_action","id":"{event_id}","when":"<e.g. start - 1h>","prompt":"p"}}'
        )

    def test_event_call_with_an_action_id_names_the_event(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()
        action = tool.service.actions.add(event_id, when="start", prompt="a", target="agent-one")

        _, added = tool.call(
            {"action": "add_action", "id": action["id"], "when": "end", "prompt": "b"}
        )
        _, updated = tool.call(
            {"action": "update", "id": action["id"], "start": "2030-01-10T16:00"}
        )

        assert len(tool.actions()) == 1
        assert tool.only_event().start_utc == "2030-01-10T14:00:00+00:00"
        assert added.endswith(f'"id":"{event_id}","when":"end","prompt":"b"}}')
        assert updated.endswith(f'"id":"{event_id}","start":"2030-01-10T16:00"}}')

    def test_missing_id_is_named_from_a_matching_title(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, deleted = tool.call({"action": "delete", "title": "dentist"})
        _, updated = tool.call(
            {"action": "update", "title": "Dentist", "start": "2030-01-10T16:00"}
        )
        _, partial = tool.call(
            {"action": "add_action", "title": "Dent", "when": "start", "prompt": "p"}
        )

        assert len(tool.events()) == 1
        assert tool.actions() == []
        assert deleted.endswith(f'Send: {{"action":"delete","id":"{event_id}"}}')
        assert f'"id":"{event_id}","title":"Dentist","start":"2030-01-10T16:00"' in updated
        assert partial.endswith(
            f'Send: {{"action":"add_action","id":"{event_id}","when":"start","prompt":"p"}}'
        )

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

        assert tool.only_event().notes is None
        assert 'update needs the event "id"' in text


class TestStandIns:
    @pytest.mark.parametrize(
        ("field", "stand_in", "wanted"),
        [
            ("prompt", "<instruction>", "Send the actual instruction the action's Run carries"),
            ("prompt", "<the prompt from this call>", "Send the actual instruction"),
            ("target", "<agent or agent@project>", "Send an existing Agent id"),
        ],
    )
    def test_stand_in_action_field_is_refused(
        self, tool: CalendarTool, field: str, stand_in: str, wanted: str
    ) -> None:
        event_id = tool.add_dentist()
        call = {"action": "add_action", "id": event_id, "when": "start - 1h", "prompt": "p"}

        _, text = tool.call({**call, field: stand_in})

        assert tool.actions() == []
        assert text.startswith(
            f'Error (invalid_arguments): calendar was not run: {field} "{stand_in}" is a '
            f"stand-in. {wanted}"
        )

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
        assert (event.title, event.notes) == ("Dentist", None)
        assert f'{field} "{stand_in}" is a stand-in.' in created
        assert f'{field} "{stand_in}" is a stand-in.' in updated

    def test_placeholder_word_prompt_counts_as_missing(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call(
            {"action": "add_action", "id": event_id, "when": "start - 1h", "prompt": "TBD"}
        )

        assert tool.actions() == []
        assert text.endswith(
            f'Send: {{"action":"add_action","id":"{event_id}","when":"start - 1h",'
            '"prompt":"<instruction>"}'
        )


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
                {"action": "add_action", "when": "2030-02-30", "prompt": "p"},
                "when must be start or end",
            ),
            (
                {"action": "add_action", "when": "start", "prompt": "p", "target": "a b"},
                "target does not identify an agent",
            ),
        ],
        ids=[
            "end-time",
            "all-day-end-time",
            "all-day-end-missing-date",
            "action-date",
            "action-target",
        ],
    )
    def test_value_naming_no_real_time_or_agent_is_refused(
        self, tool: CalendarTool, call: dict[str, Any], reason: str
    ) -> None:
        event_id = tool.add_dentist()
        if call["action"] == "add_action":
            call = {**call, "id": event_id}

        _, text = tool.call(call)

        assert _refused(text)
        assert reason in text
        assert [event.id for event in tool.events()] == [event_id]
        assert tool.actions() == []


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

    def test_update_with_an_instruction_offers_add_action(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, alone = tool.call({"action": "update", "id": event_id, "prompt": "Remind me."})
        _, mixed = tool.call(
            {
                "action": "update",
                "id": event_id,
                "prompt": "Remind me.",
                "start": "2030-01-10T16:00",
            }
        )
        _, relative = tool.call({"action": "update", "id": event_id, "when": "start - 1h"})

        assert tool.actions() == []
        assert tool.only_event().start_utc == "2030-01-10T14:00:00+00:00"
        assert alone.endswith(
            f'{{"action":"add_action","id":"{event_id}","when":"<e.g. start - 1h>",'
            '"prompt":"Remind me."}'
        )
        assert mixed.endswith(f'{{"action":"update","id":"{event_id}","start":"2030-01-10T16:00"}}')
        assert relative.endswith(
            f'{{"action":"add_action","id":"{event_id}","when":"start - 1h",'
            '"prompt":"<instruction>"}'
        )

    def test_add_action_names_event_fields_it_does_not_apply(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call(
            {
                "action": "add_action",
                "id": event_id,
                "title": "Dentist appointment",
                "notes": "Bring the card.",
                "when": "start - 1h",
                "prompt": "Remind me.",
            }
        )

        event = tool.only_event()
        assert (event.title, event.notes) == ("Dentist", None)
        assert [item["when"] for item in tool.actions()] == ["start - 1h"]
        assert (
            "title, notes were not applied: add_action works on the action, not on its event. "
            f'To change the event, send {{"action":"update","id":"{event_id}",'
            '"title":"Dentist appointment","notes":"Bring the card."}.'
        ) in text

    def test_title_that_names_the_event_needs_no_note(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call(
            {
                "action": "add_action",
                "id": event_id,
                "title": "dentist",
                "when": "start - 1h",
                "prompt": "Remind me.",
            }
        )

        assert len(tool.actions()) == 1
        assert "not applied" not in text

    def test_update_action_with_only_event_fields_names_the_event_update(
        self, tool: CalendarTool
    ) -> None:
        event_id = tool.add_dentist()
        tool.call({"action": "add_action", "id": event_id, "when": "start", "prompt": "Go."})
        [action] = tool.actions()

        _, text = tool.call(
            {
                "action": "update_action",
                "id": action["id"],
                "start": "2030-01-10T16:00",
                "when": "start - 1h",
                "timezone": "Europe/London",
            }
        )
        _, only_event = tool.call(
            {"action": "update_action", "id": action["id"], "notes": "Bring the card."}
        )

        assert tool.only_event().start_utc == "2030-01-10T14:00:00+00:00"
        assert [item["when"] for item in tool.actions()] == ["start - 1h"]
        assert (
            f'To change the event, send {{"action":"update","id":"{event_id}",'
            '"start":"2030-01-10T16:00","timezone":"Europe/London"}.'
        ) in text
        assert _refused(only_event)
        assert only_event.endswith(
            f'To change the event: Send: {{"action":"update","id":"{event_id}",'
            '"notes":"Bring the card."}'
        )
        assert tool.only_event().notes is None

    def test_delete_action_with_changes_offers_delete_or_update(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()
        tool.call({"action": "add_action", "id": event_id, "when": "start", "prompt": "Go."})
        [action] = tool.actions()

        _, changed = tool.call(
            {"action": "delete_action", "id": action["id"], "when": "start - 1h"}
        )
        _, deleted = tool.call({"action": "delete_action", "id": action["id"], "notes": "Moved."})

        assert _refused(changed)
        assert changed.endswith(
            f'{{"action":"delete_action","id":"{action["id"]}"}} or '
            f'{{"action":"update_action","id":"{action["id"]}","when":"start - 1h"}}'
        )
        assert tool.actions() == []
        assert tool.only_event().notes is None
        assert "notes was not applied: delete_action works on the action" in deleted
