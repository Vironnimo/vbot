"""Calendar lengths, time zones and action times in other forms run when their meaning is clear.

Every reading is checked through production dispatch against the stored state and the text
the Model reads; a time that names no single instant or length fails with the calls it can mean.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.core.tools.scheduling_tool_support import DENTIST_START, CalendarTool, calendar_tool


@pytest.fixture
def tool(tmp_path: Path) -> CalendarTool:
    return calendar_tool(tmp_path)


class TestLengths:
    @pytest.mark.parametrize(
        ("start", "duration", "minutes", "days"),
        [
            (DENTIST_START, "1h", 60, None),
            (DENTIST_START, "PT1H30M", 90, None),
            (DENTIST_START, "90 minutes", 90, None),
            ("2030-01-10", "3 days", None, 3),
        ],
    )
    def test_duration_texts_become_minutes_or_days(
        self, tool: CalendarTool, start: str, duration: str, minutes: int | None, days: int | None
    ) -> None:
        tool.call({"action": "create", "title": "L", "start": start, "duration": duration})

        event = tool.only_event()
        assert (event.duration_minutes, event.duration_days) == (minutes, days)

    def test_hours_field_becomes_minutes(self, tool: CalendarTool) -> None:
        tool.call({"action": "create", "title": "L", "start": DENTIST_START, "duration_hours": 1.5})

        assert tool.only_event().duration_minutes == 90

    @pytest.mark.parametrize("length", [{"duration": "1h"}, {"minutes": 60}])
    def test_time_span_on_a_date_start_offers_a_timed_event_or_whole_days(
        self, tool: CalendarTool, length: dict[str, Any]
    ) -> None:
        _, text = tool.call({"action": "create", "title": "L", "start": "2030-01-10", **length})

        assert tool.events() == []
        assert text == (
            "Error (invalid_arguments): calendar was not run: a length of 1h is a time span, but "
            "a date start makes an all-day event, which lasts whole days. Send the one that is "
            'meant: {"action":"create","title":"L","start":"2030-01-10T<HH:MM>","duration":60} '
            "(a timed event; put its start time in place of <HH:MM>) or "
            '{"action":"create","title":"L","start":"2030-01-10","duration":1} (all day, 1 day)'
        )

    def test_unreadable_duration_is_refused_with_a_stand_in(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {"action": "create", "title": "L", "start": DENTIST_START, "duration": "a while"}
        )

        assert tool.events() == []
        assert text == (
            'Error (invalid_arguments): calendar was not run: duration "a while" is not a '
            "length. Send a whole number: minutes for a timed event, days for an all-day event. "
            'Send: {"action":"create","title":"L","start":"2030-01-10T15:00",'
            '"duration":"<minutes, or days for all-day>"}'
        )

    @pytest.mark.parametrize(
        ("start", "minutes", "until"),
        [
            (DENTIST_START, 2880, "2030-01-12T15:00"),
            # Berlin moves its clocks forward on 2030-03-31: two days are 47 hours.
            ("2030-03-30T10:00", 2820, "2030-04-01T10:00"),
        ],
    )
    def test_days_for_a_timed_start_become_minutes_to_the_same_clock_time(
        self, tool: CalendarTool, start: str, minutes: int, until: str
    ) -> None:
        _, text = tool.call({"action": "create", "title": "L", "start": start, "days": 2})

        assert tool.only_event().duration_minutes == minutes
        assert (
            f"note: A timed event lasts minutes: read 2 days as {minutes} minutes, so it ends "
            f"at {until}."
        ) in text

    def test_days_for_a_date_start_are_the_all_day_length(self, tool: CalendarTool) -> None:
        tool.call({"action": "create", "title": "L", "start": "2030-01-10", "days": 3})

        assert tool.only_event().duration_days == 3

    @pytest.mark.parametrize(
        ("start", "change", "minutes", "days"),
        [
            (DENTIST_START, {"duration": "1.5h"}, 90, None),
            (DENTIST_START, {"duration": "2d"}, 2880, None),
            ("2030-01-10", {"duration": "2d"}, None, 2),
            ("2030-01-10", {"duration": "P1W"}, None, 7),
            ("2030-01-10", {"minutes": 2880}, None, 2),
        ],
    )
    def test_length_with_a_unit_on_update_reads_against_the_stored_start(
        self,
        tool: CalendarTool,
        start: str,
        change: dict[str, Any],
        minutes: int | None,
        days: int | None,
    ) -> None:
        event_id = tool.service.create_event(title="L", start=start).id

        tool.call({"action": "update", "id": event_id, **change})

        event = tool.only_event()
        assert (event.duration_minutes, event.duration_days) == (minutes, days)

    def test_minutes_on_update_of_an_all_day_event_are_refused(self, tool: CalendarTool) -> None:
        event_id = tool.service.create_event(title="L", start="2030-01-10").id

        _, text = tool.call({"action": "update", "id": event_id, "minutes": 90})

        assert tool.only_event().duration_days == 1
        assert text.endswith(
            f'{{"action":"update","id":"{event_id}","start":"2030-01-10T<HH:MM>","duration":90}} '
            "(a timed event; put its start time in place of <HH:MM>) or "
            f'{{"action":"update","id":"{event_id}","duration":1}} (all day, 1 day)'
        )

    def test_lengths_that_disagree_offer_each(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "L",
                "start": DENTIST_START,
                "duration": 60,
                "minutes": 90,
            }
        )

        assert tool.events() == []
        assert text.endswith(
            'Send the one that is meant: {"action":"create","title":"L",'
            '"start":"2030-01-10T15:00","duration":60} or {"action":"create","title":"L",'
            '"start":"2030-01-10T15:00","duration":90}'
        )

    def test_find_free_reads_days_as_minutes(self, tool: CalendarTool) -> None:
        _, text = tool.call({"action": "find_free", "when": "2030-01-07", "duration": "1d"})

        assert "note: Read 1 day as 1440 minutes." in text

    @pytest.mark.parametrize(
        ("start", "end", "minutes"),
        [
            (DENTIST_START, "2030-01-10T16:30", 90),
            (DENTIST_START, "16:00", 60),
            ("2030-01-10T23:00", "01:00", 120),
        ],
    )
    def test_end_time_becomes_the_duration(
        self, tool: CalendarTool, start: str, end: str, minutes: int
    ) -> None:
        tool.call({"action": "create", "title": "E", "start": start, "end": end})

        assert tool.only_event().duration_minutes == minutes

    def test_all_day_end_on_the_start_day_is_one_day(self, tool: CalendarTool) -> None:
        tool.call({"action": "create", "title": "E", "start": "2030-01-10", "end": "2030-01-10"})

        assert tool.only_event().duration_days == 1

    def test_later_all_day_end_date_offers_both_readings(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {"action": "create", "title": "E", "start": "2030-01-10", "end": "2030-01-12"}
        )

        assert tool.events() == []
        assert '"duration":3} (through 2030-01-12) or ' in text
        assert '"duration":2} (ending before 2030-01-12)' in text

    def test_end_that_disagrees_with_duration_offers_both(self, tool: CalendarTool) -> None:
        tool.call(
            {
                "action": "create",
                "title": "Same",
                "start": DENTIST_START,
                "end": "16:00",
                "duration": 60,
            }
        )
        _, text = tool.call(
            {
                "action": "create",
                "title": "E",
                "start": DENTIST_START,
                "end": "16:00",
                "duration": 30,
            }
        )

        assert [event.title for event in tool.events()] == ["Same"]
        assert '"end" gives a duration of 60, but "duration" is 30.' in text

    def test_end_on_update_measures_from_the_stored_start(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        tool.call({"action": "update", "id": event_id, "end": "17:15"})

        assert tool.only_event().duration_minutes == 135


class TestTimeZones:
    def test_single_event_in_another_zone_is_converted_with_a_note(
        self, tool: CalendarTool
    ) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "Call",
                "start": "2030-01-12T10:00",
                "end": "11:00",
                "timezone": "America/New_York",
            }
        )

        event = tool.only_event()
        assert (event.start_utc, event.duration_minutes) == ("2030-01-12T15:00:00+00:00", 60)
        assert (
            'note: Read "2030-01-12T10:00" and "2030-01-12T11:00" as America/New_York time: '
            "2030-01-12T16:00 and 2030-01-12T17:00 in the server time zone Europe/Berlin."
        ) in text

    def test_server_zone_in_any_spelling_changes_nothing(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {"action": "create", "title": "C", "start": DENTIST_START, "timezone": "europe/berlin"}
        )

        assert tool.only_event().start_utc == "2030-01-10T14:00:00+00:00"
        assert "note" not in text

    def test_unknown_zone_is_refused(self, tool: CalendarTool) -> None:
        _, text = tool.call(
            {"action": "create", "title": "C", "start": DENTIST_START, "timezone": "Mars/Base"}
        )

        assert tool.events() == []
        assert '"timezone" "Mars/Base" is not a known time zone.' in text

    def test_repeating_event_across_changing_offsets_is_refused_with_a_start(
        self, tool: CalendarTool
    ) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "W",
                "start": "2030-01-13T10:00",
                "rrule": "weekly",
                "timezone": "America/New_York",
            }
        )

        assert tool.events() == []
        assert "offset that changes during the year" in text
        assert text.endswith(
            'Send: {"action":"create","title":"W","start":"2030-01-13T16:00",'
            '"rrule":{"freq":"weekly"}}'
        )

    def test_repeating_event_with_a_constant_offset_is_converted(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path, tz="UTC")

        tool.call(
            {
                "action": "create",
                "title": "W",
                "start": "2030-01-13T10:00",
                "rrule": "weekly",
                "timezone": "Asia/Tokyo",
            }
        )

        assert tool.only_event().start_local == "2030-01-13T01:00:00"

    def test_repeating_event_from_a_skipped_time_keeps_its_wall_clock_time(
        self, tool: CalendarTool
    ) -> None:
        # London and Berlin change their clocks at the same instants, one hour apart all year.
        # The first 01:30 falls in London's spring gap; the following Sundays keep 01:30.
        tool.call(
            {
                "action": "create",
                "title": "W",
                "start": "2030-03-31T01:30",
                "rrule": "weekly",
                "timezone": "Europe/London",
            }
        )

        assert tool.only_event().start_local == "2030-03-31T02:30:00"

    def test_repeating_event_that_moves_to_another_day_is_refused(self, tmp_path: Path) -> None:
        tool = calendar_tool(tmp_path, tz="UTC")

        _, text = tool.call(
            {
                "action": "create",
                "title": "W",
                "start": "2030-01-13T08:00",
                "rrule": "weekly",
                "timezone": "Asia/Tokyo",
            }
        )

        assert tool.events() == []
        assert "falls on another day there" in text
        assert '"start":"2030-01-12T23:00"' in text

    def test_start_with_an_offset_of_another_zone_offers_both_readings(
        self, tool: CalendarTool
    ) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "C",
                "start": "2030-01-10T15:00+05:00",
                "timezone": "America/New_York",
            }
        )

        assert tool.events() == []
        assert text.endswith(
            'Send the one that is meant: {"action":"create","title":"C",'
            '"start":"2030-01-10T05:00-05:00","timezone":"America/New_York"} '
            '(2030-01-10T15:00+05:00 as written) or {"action":"create","title":"C",'
            '"start":"2030-01-10T15:00-05:00","timezone":"America/New_York"} '
            "(2030-01-10T15:00 in America/New_York)"
        )

        tool.call(
            {
                "action": "create",
                "title": "C",
                "start": "2030-01-10T05:00-05:00",
                "timezone": "America/New_York",
            }
        )

        assert tool.only_event().start_utc == "2030-01-10T10:00:00+00:00"

    @pytest.mark.parametrize(
        ("start", "reason", "meant"),
        [
            (
                "2030-03-31T01:30",
                "does not exist in Europe/London: the clocks jump over it that day.",
                ["2030-03-31T00:30+00:00", "2030-03-31T02:30+01:00"],
            ),
            (
                "2030-10-27T01:30",
                "happens twice in Europe/London: the clocks go back over it that day.",
                ["2030-10-27T01:30+01:00", "2030-10-27T01:30+00:00"],
            ),
        ],
    )
    def test_local_time_the_zone_skips_or_repeats_is_refused_with_each_instant(
        self, tool: CalendarTool, start: str, reason: str, meant: list[str]
    ) -> None:
        call = {
            "action": "create",
            "title": "C",
            "start": start,
            "duration": 90,
            "timezone": "Europe/London",
        }

        _, text = tool.call(call)

        assert tool.events() == []
        sends = " or ".join(
            f'{{"action":"create","title":"C","start":"{moment}","duration":90,'
            f'"timezone":"Europe/London"}} ({moment} in Europe/London)'
            for moment in meant
        )
        assert text == (
            f'Error (invalid_arguments): calendar was not run: "{start}" {reason} '
            f"Send the one that is meant: {sends}"
        )

        tool.call({**call, "start": meant[1]})

        event = tool.only_event()
        expected = datetime.fromisoformat(meant[1]).astimezone(UTC).isoformat()
        assert (event.start_utc, event.duration_minutes) == (expected, 90)

    def test_end_in_the_repeated_hour_is_refused_with_each_instant(
        self, tool: CalendarTool
    ) -> None:
        _, text = tool.call(
            {
                "action": "create",
                "title": "C",
                "start": "2030-10-27T00:30",
                "end": "2030-10-27T01:30",
                "timezone": "Europe/London",
            }
        )

        assert tool.events() == []
        assert '"2030-10-27T01:30" happens twice in Europe/London' in text
        assert '"end":"2030-10-27T01:30+01:00"' in text
        assert '"end":"2030-10-27T01:30+00:00"' in text

    def test_all_day_event_ignores_the_zone(self, tool: CalendarTool) -> None:
        tool.call(
            {
                "action": "create",
                "title": "Day",
                "start": "2030-01-10",
                "timezone": "America/New_York",
            }
        )

        assert tool.only_event().start_date == "2030-01-10"

    def test_list_window_is_read_in_the_named_zone(self, tool: CalendarTool) -> None:
        tool.add_dentist()

        _, text = tool.call(
            {"action": "list", "when": "2030-01-10", "timezone": "America/New_York"}
        )

        assert "window: 2030-01-10T06:00 to 2030-01-11T06:00" in text
        assert "note: Read the window in America/New_York" in text

    def test_occurrence_start_in_another_zone_removes_that_occurrence(
        self, tool: CalendarTool
    ) -> None:
        weekly_id = tool.add_weekly()

        tool.call(
            {
                "action": "delete",
                "id": weekly_id,
                "start": "2030-01-14T08:00",
                "timezone": "Europe/London",
            }
        )

        assert tool.only_event().exdates == ["2030-01-14T09:00:00"]


class TestActionTimes:
    @pytest.mark.parametrize(
        ("call", "stored"),
        [
            ({"when": "1h before"}, "start - 1h"),
            ({"when": "-30m"}, "start - 30m"),
            ({"when": "at start"}, "start"),
            ({"when": "30 minutes after end"}, "end + 30m"),
            ({"when": "START-2H"}, "start - 2h"),
            ({"minutes_before": 15}, "start - 15m"),
            ({"when": -20}, "start - 20m"),
        ],
    )
    def test_reminder_phrasings_become_relative_times(
        self, tool: CalendarTool, call: dict[str, Any], stored: str
    ) -> None:
        event_id = tool.add_dentist()

        tool.call({"action": "add_action", "id": event_id, "prompt": "p", **call})

        assert [action["when"] for action in tool.actions()] == [stored]

    @pytest.mark.parametrize("when", ["+15m", "15 min after", 10, "10"])
    def test_offsets_without_an_anchor_offer_start_or_end(
        self, tool: CalendarTool, when: Any
    ) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call({"action": "add_action", "id": event_id, "when": when, "prompt": "p"})

        assert tool.actions() == []
        assert '"when":"start' in text
        assert '"when":"end + ' in text

    def test_clock_time_for_a_single_event_becomes_relative(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call(
            {"action": "add_action", "id": event_id, "when": "2030-01-10T14:30", "prompt": "p"}
        )

        assert [action["when"] for action in tool.actions()] == ["start - 30m"]
        assert 'note: Read "2030-01-10T14:30" as "start - 30m".' in text

    def test_clock_time_in_another_zone_is_read_there(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        tool.call(
            {
                "action": "add_action",
                "id": event_id,
                "when": "2030-01-10T08:00",
                "timezone": "America/New_York",
                "prompt": "p",
            }
        )

        assert [action["when"] for action in tool.actions()] == ["start - 1h"]

    def test_clock_time_the_named_zone_skips_is_refused_with_each_instant(
        self, tool: CalendarTool
    ) -> None:
        event_id = tool.service.create_event(title="Late", start="2030-03-31T05:00").id

        _, text = tool.call(
            {
                "action": "add_action",
                "id": event_id,
                "when": "2030-03-31T01:30",
                "timezone": "Europe/London",
                "prompt": "p",
            }
        )

        assert tool.actions() == []
        assert '"2030-03-31T01:30" does not exist in Europe/London' in text
        assert '"when":"2030-03-31T00:30+00:00"' in text
        assert '"when":"2030-03-31T02:30+01:00"' in text

        tool.call(
            {
                "action": "add_action",
                "id": event_id,
                "when": "2030-03-31T02:30+01:00",
                "timezone": "Europe/London",
                "prompt": "p",
            }
        )

        # 02:30 in London is 03:30 in Berlin, 90 minutes before the 05:00 start.
        assert [action["when"] for action in tool.actions()] == ["start - 90m"]

    def test_clock_time_for_a_repeating_event_is_refused_with_a_relative_one(
        self, tool: CalendarTool
    ) -> None:
        weekly_id = tool.add_weekly()

        _, text = tool.call(
            {"action": "add_action", "id": weekly_id, "when": "2030-01-14T08:30", "prompt": "p"}
        )

        assert tool.actions() == []
        assert text.endswith(
            f'Send: {{"action":"add_action","id":"{weekly_id}","when":"start - 30m","prompt":"p"}}'
        )

    def test_current_session_reads_as_this_session_and_new_as_fresh(
        self, tool: CalendarTool
    ) -> None:
        event_id = tool.add_dentist()

        tool.call(
            {
                "action": "add_action",
                "id": event_id,
                "when": "start",
                "prompt": "a",
                "session": "current",
            }
        )
        tool.call(
            {"action": "add_action", "id": event_id, "when": "end", "prompt": "b", "session": "new"}
        )

        sessions = {action["prompt"]: action["session"] for action in tool.actions()}
        assert sessions == {"a": "session-one", "b": None}

    def test_current_session_for_another_target_is_refused(self, tool: CalendarTool) -> None:
        event_id = tool.add_dentist()

        _, text = tool.call(
            {
                "action": "add_action",
                "id": event_id,
                "when": "start",
                "prompt": "p",
                "session": "current",
                "target": "other",
            }
        )

        assert tool.actions() == []
        assert "the current Session belongs to agent-one, not to the target other." in text
