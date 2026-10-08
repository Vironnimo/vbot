"""The calendar Tool through production dispatch: canonical calls, state, and result text."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.tools.calendar import CALENDAR_TOOL_NAME, CALENDAR_TOOL_PARAMETERS
from tests.core.tools.scheduling_tool_support import CalendarTool, calendar_tool, clock_at

WEEKLY = "FREQ=WEEKLY;BYDAY=MO,WE"


@pytest.fixture
def tool(tmp_path: Path) -> CalendarTool:
    return calendar_tool(tmp_path)


def _standup(tool: CalendarTool) -> str:
    """Mondays and Wednesdays 09:00-09:15 from 2030-01-07, in Room 4; return its id."""
    return tool.service.create_event(
        title="Standup",
        start="2030-01-07T09:00",
        end="2030-01-07T09:15",
        rrule=WEEKLY,
        location="Room 4",
    ).id


def _bind_job(tool: CalendarTool, event_id: str, schedule: str = "start - 30m") -> str:
    parsed = tool.cron.parse_event_schedule(event_id, schedule)
    job = asyncio.run(
        tool.cron.create_job(
            agent_id="agent-one",
            name="Agenda",
            prompt="Post the agenda.",
            schedule_type="event",
            event_id=parsed.event_id,
            event_edge=parsed.event_edge,
            event_offset_minutes=parsed.event_offset_minutes,
        )
    )
    return job.id


def _now(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr("core.tools.calendar.datetime", clock_at(moment))


def test_definition_has_the_canonical_parameters() -> None:
    properties = CALENDAR_TOOL_PARAMETERS["properties"]
    assert list(properties) == [
        "action",
        "id",
        "title",
        "start",
        "end",
        "description",
        "location",
        "rrule",
        "time_min",
        "time_max",
        "query",
        "duration",
    ]
    assert properties["action"]["enum"] == ["list", "create", "update", "delete", "find_free_time"]
    assert (properties["duration"]["type"], properties["duration"]["default"]) == ("integer", 60)
    assert CALENDAR_TOOL_PARAMETERS["required"] == ["action"]


class TestCreate:
    def test_timed_event_is_local_time_and_lasts_an_hour_without_end(
        self, tool: CalendarTool
    ) -> None:
        text = tool.succeeded(
            {
                "action": "create",
                "title": "Dentist",
                "start": "2030-01-10T15:00",
                "location": "Dr. Weiss",
                "description": "Bring the card.",
            }
        )

        event = tool.only_event()
        assert (event.start, event.end, event.tz_name) == (
            "2030-01-10T15:00:00",
            "2030-01-10T16:00:00",
            "Europe/Berlin",
        )
        assert text == (
            f"id: {event.id}\ntitle: Dentist\nstart: 2030-01-10T15:00\nend: 2030-01-10T16:00\n"
            "location: Dr. Weiss\ndescription: Bring the card."
        )

    @pytest.mark.parametrize(
        ("end", "stored", "shown"),
        [
            (None, "2030-01-11", "2030-01-11 (last day 2030-01-10)"),
            ("2030-01-13", "2030-01-13", "2030-01-13 (last day 2030-01-12)"),
            # An end on the start day means that one day.
            ("2030-01-10", "2030-01-11", "2030-01-11 (last day 2030-01-10)"),
        ],
    )
    def test_all_day_end_is_exclusive_and_the_result_names_the_last_day(
        self, tool: CalendarTool, end: str | None, stored: str, shown: str
    ) -> None:
        call = {"action": "create", "title": "Holiday", "start": "2030-01-10"}
        text = tool.succeeded(call if end is None else {**call, "end": end})

        event = tool.only_event()
        assert (event.start, event.end, event.all_day) == ("2030-01-10", stored, True)
        assert f"end: {shown}" in text

    def test_repeating_event_shows_its_rule_and_next_starts(
        self, tool: CalendarTool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _now(monkeypatch, datetime(2030, 1, 8, 12, tzinfo=UTC))

        text = tool.succeeded(
            {"action": "create", "title": "Standup", "start": "2030-01-07T09:00", "rrule": WEEKLY}
        )

        assert tool.only_event().rrule == WEEKLY
        assert f"rrule: {WEEKLY}" in text
        assert "next: 2030-01-09T09:00, 2030-01-14T09:00, 2030-01-16T09:00" in text

    def test_create_needs_title_and_start(self, tool: CalendarTool) -> None:
        message = tool.refused({"action": "create", "location": "Home"})

        assert message == (
            "calendar was not run: create needs title and start. Send: "
            '{"action":"create","title":"<title>","start":"<2030-01-10T15:00 or 2030-01-10>",'
            '"location":"Home"}'
        )

    def test_invalid_rule_is_refused(self, tool: CalendarTool) -> None:
        message = tool.refused(
            {
                "action": "create",
                "title": "X",
                "start": "2030-01-07T09:00",
                "rrule": "FREQ=SOMETIMES",
            }
        )

        assert message.startswith("calendar was not run: ")
        assert '"rrule":"<rule such as FREQ=WEEKLY;BYDAY=MO>"' in message


class TestList:
    def test_list_shows_a_header_and_one_block_per_event(self, tool: CalendarTool) -> None:
        standup = _standup(tool)
        job = _bind_job(tool, standup)
        asyncio.run(
            tool.service.update_occurrence(f"{standup}_20300109T0900", start="2030-01-09T10:00")
        )
        trip = tool.service.create_event(title="Trip", start="2030-01-14", end="2030-01-17").id

        envelope, text = tool.call(
            {"action": "list", "time_min": "2030-01-06", "time_max": "2030-01-20"}
        )

        assert envelope["data"]["events"] == 2
        assert text == (
            "events: 2\nwindow: 2030-01-06 to 2030-01-19\ntimezone: Europe/Berlin\n\n"
            f"id: {standup}\ntitle: Standup\nstart: 2030-01-07T09:00\nend: 2030-01-07T09:15\n"
            f"rrule: {WEEKLY}\nlocation: Room 4\n"
            f"cron_jobs: {job} at start - 30m, target agent-one\n"
            "occurrences:\n"
            f"  {standup}_20300107T0900 2030-01-07T09:00\n"
            f"  {standup}_20300109T0900 2030-01-09T10:00 to 2030-01-09T10:15\n"
            f"  {standup}_20300114T0900 2030-01-14T09:00\n"
            f"  {standup}_20300116T0900 2030-01-16T09:00\n\n"
            f"id: {trip}\ntitle: Trip\nstart: 2030-01-14\nend: 2030-01-17 (last day 2030-01-16)"
        )

    def test_window_starts_now_and_spans_30_days_by_default(
        self, tool: CalendarTool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _now(monkeypatch, datetime(2030, 1, 1, 11, tzinfo=UTC))  # 12:00 in Berlin
        for title, start in [
            ("Over", "2030-01-01T11:00"),
            ("Last inside", "2030-01-31T11:00"),
            ("After", "2030-01-31T12:00"),
        ]:
            tool.service.create_event(title=title, start=start)

        text = tool.succeeded({"action": "list"})

        assert "window: 2030-01-01T12:00 to 2030-01-31T12:00" in text
        assert "title: Last inside" in text
        assert "Over" not in text and "After" not in text

    @pytest.mark.parametrize(
        ("time_min", "time_max", "titles"),
        [
            ("2030-01-10", "2030-01-11", ["Morning", "Evening"]),
            ("2030-01-10T09:00", "2030-01-10T18:00", ["Morning"]),
        ],
    )
    def test_time_max_is_exclusive(
        self, tool: CalendarTool, time_min: str, time_max: str, titles: list[str]
    ) -> None:
        tool.service.create_event(title="Morning", start="2030-01-10T09:00")
        tool.service.create_event(title="Evening", start="2030-01-10T18:00")
        tool.service.create_event(title="Next day", start="2030-01-11")

        text = tool.succeeded({"action": "list", "time_min": time_min, "time_max": time_max})

        assert [line[7:] for line in text.splitlines() if line.startswith("title: ")] == titles

    def test_query_finds_text_in_title_description_and_location(self, tool: CalendarTool) -> None:
        for fields in [
            {"title": "Piano lesson"},
            {"title": "Lesson", "description": "Bring the PIANO book."},
            {"title": "Concert", "location": "Piano hall"},
            {"title": "Dentist"},
        ]:
            tool.service.create_event(start="2030-01-10T15:00", **fields)

        envelope, text = tool.call({"action": "list", "time_min": "2030-01-10", "query": "piano"})

        assert envelope["data"]["events"] == 3
        assert "query: piano" in text
        assert "Dentist" not in text

    def test_a_long_series_lists_its_first_occurrences(self, tool: CalendarTool) -> None:
        event_id = tool.service.create_event(
            title="Daily", start="2030-01-01T08:00", rrule="FREQ=DAILY"
        ).id

        text = tool.succeeded(
            {"action": "list", "time_min": "2030-01-01", "time_max": "2030-01-31"}
        )

        occurrences = text.split("occurrences:\n", 1)[1].splitlines()
        assert occurrences[0] == f"  {event_id}_20300101T0800 2030-01-01T08:00"
        assert occurrences[10:] == [
            "  ...20 more, the last at 2030-01-30T08:00; a shorter window lists them"
        ]

    @pytest.mark.parametrize(
        ("window", "message"),
        [
            (
                {"time_min": "2030-01-10", "time_max": "2030-01-10"},
                'calendar was not run: time_max must come after time_min. Send: {"action":"list",'
                '"time_min":"2030-01-10","time_max":"<2030-01-10 or 2030-01-10T15:00>"}',
            ),
            (
                {"time_min": "2030-01-01", "time_max": "2030-06-01"},
                'calendar was not run: a window spans at most 62 days. Send: {"action":"list",'
                '"time_min":"2030-01-01T00:00","time_max":"2030-03-04T00:00"}',
            ),
            (
                {"time_min": "next tuesday"},
                'calendar was not run: time_min "next tuesday" is not a date or local date-time. '
                'Send: {"action":"list","time_min":"<2030-01-10 or 2030-01-10T15:00>"}',
            ),
        ],
    )
    def test_invalid_window_is_refused(
        self, tool: CalendarTool, window: dict[str, str], message: str
    ) -> None:
        assert tool.refused({"action": "list", **window}) == message


class TestFindFreeTime:
    def test_lists_free_spans_of_at_least_duration_minutes(self, tool: CalendarTool) -> None:
        tool.service.create_event(title="A", start="2030-01-07T09:00", end="2030-01-07T10:00")
        tool.service.create_event(title="B", start="2030-01-07T10:30", end="2030-01-07T11:00")

        text = tool.succeeded(
            {
                "action": "find_free_time",
                "time_min": "2030-01-07T08:00",
                "time_max": "2030-01-07T18:00",
                "duration": 60,
            }
        )

        assert text == (
            "free: 2\nwindow: 2030-01-07T08:00 to 2030-01-07T18:00\ntimezone: Europe/Berlin\n\n"
            "2030-01-07T08:00 to 2030-01-07T09:00 (1h)\n"
            "2030-01-07T11:00 to 2030-01-07T18:00 (7h)"
        )

    def test_defaults_to_seven_days_and_an_hour(
        self, tool: CalendarTool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _now(monkeypatch, datetime(2030, 1, 1, 11, tzinfo=UTC))
        # Busy all week but for 60 minutes on the 3rd and 59 on the 5th.
        tool.service.create_event(title="Busy", start="2030-01-01T12:00", end="2030-01-03T10:00")
        tool.service.create_event(title="Busy", start="2030-01-03T11:00", end="2030-01-05T10:00")
        tool.service.create_event(title="Busy", start="2030-01-05T10:59", end="2030-01-09T00:00")

        text = tool.succeeded({"action": "find_free_time"})

        assert text == (
            "free: 1\nwindow: 2030-01-01T12:00 to 2030-01-08T12:00\ntimezone: Europe/Berlin\n\n"
            "2030-01-03T10:00 to 2030-01-03T11:00 (1h)"
        )

    def test_no_free_span_is_named(self, tool: CalendarTool) -> None:
        tool.service.create_event(title="Busy", start="2030-01-07")

        text = tool.succeeded(
            {"action": "find_free_time", "time_min": "2030-01-07", "time_max": "2030-01-08"}
        )

        assert "free: 0" in text
        assert "note: No free span of 1h or more in this window." in text


class TestUpdate:
    def test_update_changes_only_the_fields_sent(self, tool: CalendarTool) -> None:
        event = tool.service.create_event(
            title="Dentist", start="2030-01-10T15:00", location="Dr. Weiss", description="Card."
        )

        text = tool.succeeded({"action": "update", "id": event.id, "title": "Dentist checkup"})

        updated = tool.only_event()
        assert updated.title == "Dentist checkup"
        assert (updated.start, updated.end, updated.location, updated.description) == (
            event.start,
            event.end,
            event.location,
            event.description,
        )
        assert "title: Dentist checkup" in text

    def test_a_new_start_alone_keeps_the_length(self, tool: CalendarTool) -> None:
        event = tool.service.create_event(
            title="Workshop", start="2030-01-10T15:00", end="2030-01-10T17:30"
        )

        tool.succeeded({"action": "update", "id": event.id, "start": "2030-01-11T09:00"})

        assert (tool.only_event().start, tool.only_event().end) == (
            "2030-01-11T09:00:00",
            "2030-01-11T11:30:00",
        )

    def test_empty_rrule_stops_repeating(self, tool: CalendarTool) -> None:
        standup = _standup(tool)

        text = tool.succeeded({"action": "update", "id": standup, "rrule": ""})

        assert tool.only_event().rrule is None
        assert "rrule" not in text and "next" not in text

    @pytest.mark.parametrize("action", ["update", "delete"])
    def test_an_occurrence_id_changes_or_deletes_only_that_occurrence(
        self, tool: CalendarTool, action: str
    ) -> None:
        standup = _standup(tool)
        changes = {"title": "Long standup", "end": "2030-01-09T10:00"} if action == "update" else {}

        text = tool.succeeded({"action": action, "id": f"{standup}_20300109T0900", **changes})

        listing = tool.succeeded(
            {"action": "list", "time_min": "2030-01-07", "time_max": "2030-01-15"}
        )
        if action == "update":
            assert text == (
                f"id: {standup}_20300109T0900\nseries: {standup}\ntitle: Long standup\n"
                "start: 2030-01-09T09:00\nend: 2030-01-09T10:00\nlocation: Room 4"
            )
            assert (
                f"  {standup}_20300109T0900 2030-01-09T09:00 to 2030-01-09T10:00, "
                "title: Long standup"
            ) in listing
        else:
            assert text == (
                f"id: {standup}_20300109T0900\ntitle: Standup\nstart: 2030-01-09T09:00\n"
                "status: deleted; the rest of the series stays"
            )
            assert f"{standup}_20300109T0900" not in listing
        assert f"  {standup}_20300107T0900 2030-01-07T09:00\n" in listing
        assert f"  {standup}_20300114T0900 2030-01-14T09:00" in listing
        assert tool.only_event().title == "Standup"

    def test_rule_of_an_occurrence_goes_to_the_series(self, tool: CalendarTool) -> None:
        standup = _standup(tool)

        message = tool.refused(
            {"action": "update", "id": f"{standup}_20300109T0900", "rrule": "FREQ=DAILY"}
        )

        assert message == (
            "calendar was not run: rrule belongs to the whole series, so it takes the event id. "
            f'Send: {{"action":"update","id":"{standup}","rrule":"FREQ=DAILY"}}'
        )

    def test_update_needs_a_change(self, tool: CalendarTool) -> None:
        event_id = tool.service.create_event(title="Dentist", start="2030-01-10T15:00").id

        message = tool.refused({"action": "update", "id": event_id})

        assert message == (
            "calendar was not run: update needs a field to change: title, start, end, "
            f'description, location or rrule. Send: {{"action":"update","id":"{event_id}",'
            '"start":"<2030-01-10T15:00 or 2030-01-10>"}'
        )


class TestIds:
    @pytest.mark.parametrize(
        ("call", "message"),
        [
            (
                {"action": "update", "title": "Dentist", "start": "2030-01-10T16:00"},
                'calendar was not run: update needs the event "id"; {"action":"list",'
                '"query":"Dentist"} shows events and their ids. Send: {"action":"update",'
                '"id":"<event id from list>","title":"Dentist","start":"2030-01-10T16:00"}',
            ),
            (
                {"action": "delete"},
                'calendar was not run: delete needs the event "id"; {"action":"list"} shows '
                'events and their ids. Send: {"action":"delete","id":"<event id from list>"}',
            ),
        ],
    )
    def test_update_and_delete_need_an_id(
        self, tool: CalendarTool, call: dict[str, Any], message: str
    ) -> None:
        assert tool.refused(call) == message

    def test_unknown_event_names_the_list_call(self, tool: CalendarTool) -> None:
        envelope, _text = tool.call({"action": "delete", "id": "evt_missing"})

        assert envelope["error"] == {
            "code": "event_not_found",
            "message": (
                'No event has id "evt_missing". {"action":"list"} shows events and their ids.'
            ),
        }

    def test_unknown_occurrence_names_the_list_call_that_shows_the_series(
        self, tool: CalendarTool
    ) -> None:
        standup = _standup(tool)

        envelope, _text = tool.call({"action": "delete", "id": f"{standup}_20300108T0900"})

        listing = f'{{"action":"list","id":"{standup}"}}'
        assert envelope["error"]["message"] == (
            f'No occurrence has id "{standup}_20300108T0900". {listing} shows the event\'s '
            "occurrences."
        )
        assert f"{standup}_20300107T0900" in tool.succeeded(
            {"action": "list", "id": standup, "time_min": "2030-01-01"}
        )


def test_delete_reports_the_cron_jobs_it_removed(tool: CalendarTool) -> None:
    standup = _standup(tool)
    job = _bind_job(tool, standup)

    text = tool.succeeded({"action": "delete", "id": standup})

    assert tool.events() == [] and tool.cron.list_jobs() == []
    assert text == (
        f"id: {standup}\ntitle: Standup\nstatus: deleted with all its occurrences\n"
        f'deleted_cron_jobs: {job} "Agenda"'
    )


@pytest.mark.parametrize("action", ["update", "delete"])
def test_changes_wait_for_the_reference_lock(tool: CalendarTool, action: str) -> None:
    event_id = tool.service.create_event(title="Dentist", start="2030-01-10T15:00").id
    call = (
        {"action": action, "id": event_id, "title": "Moved"}
        if action == "update"
        else {
            "action": action,
            "id": event_id,
        }
    )

    async def scenario() -> None:
        async with tool.reference_lock:
            task = asyncio.create_task(tool.call_async(call))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            assert tool.only_event().title == "Dentist"
        envelope, _text = await task
        assert envelope["ok"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("arguments", "labels"),
    [
        ({"summary": "Dentist", "start": "2030-01-10T15:00"}, ["create", "Dentist"]),
        ({"action": "list", "q": "piano"}, ["list", "piano"]),
        # Calls persisted before the calendar lost its actions still render.
        ({"action": "add_action", "id": "evt_1", "prompt": "Remind me."}, ["update", "evt_1"]),
        ({"action": "update_action", "id": "evt_1", "action_id": "act_1"}, []),
        (
            {"action": "find_free", "when": "this week", "duration": 30},
            ["find_free_time", "this week"],
        ),
    ],
)
def test_display_labels_the_meant_action(
    tool: CalendarTool, arguments: dict[str, Any], labels: list[str]
) -> None:
    display = tool.registry.display_for_call(CALENDAR_TOOL_NAME, arguments)

    assert [part["value"] for part in display.get("primary", [])] == labels
