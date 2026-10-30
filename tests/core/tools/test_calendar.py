"""The calendar Tool through production dispatch: canonical calls, state, and result text."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.calendar import CalendarService, occurrence_id
from core.tools._calendar_arguments import STAND_INS
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
        "timezone",
        "rrule",
        "time_min",
        "time_max",
        "query",
        "duration",
        "cursor",
    ]
    assert properties["action"]["enum"] == [
        "list",
        "get",
        "search",
        "create",
        "update",
        "delete",
        "find_free_time",
    ]
    assert (properties["duration"]["type"], properties["duration"]["default"]) == ("integer", 60)
    assert CALENDAR_TOOL_PARAMETERS["required"] == ["action"]


class TestCreate:
    def test_timed_event_is_local_time_and_lasts_an_hour_without_end(
        self, tool: CalendarTool
    ) -> None:
        result, text = tool.call(
            {
                "action": "create",
                "title": "Dentist",
                "start": "2030-01-10T15:00",
                "location": "Dr. Weiss",
                "description": "Bring the card.",
            }
        )
        assert result["ok"], text
        event = tool.only_event()
        assert (event.start, event.end, event.tz_name) == (
            "2030-01-10T15:00:00",
            "2030-01-10T16:00:00",
            "Europe/Berlin",
        )
        assert result["data"] == {
            "id": event.id,
            "title": "Dentist",
            "start": "2030-01-10T15:00+01:00",
            "end": "2030-01-10T16:00+01:00",
            "timezone": "Europe/Berlin",
            "location": "Dr. Weiss",
            "description": "Bring the card.",
        }

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
        event = tool.only_event()
        assert f"{event.id}_20300109T0900 2030-01-09T09:00+01:00" in text
        assert f"{event.id}_20300116T0900 2030-01-16T09:00+01:00" in text

    def test_create_needs_title_and_start(self, tool: CalendarTool) -> None:
        message = tool.refused({"action": "create", "location": "Home"})

        assert message == (
            "calendar was not run: create needs title and start. Send: "
            '{"action":"create","title":"<title>","start":"<2030-01-10T15:00 or 2030-01-10>",'
            '"location":"Home"}'
        )

    @pytest.mark.parametrize(
        ("call", "field", "value"),
        [
            (
                {"action": "create", "title": "X", "start": "next tuesday 2pm"},
                "start",
                "next tuesday 2pm",
            ),
            (
                {"action": "create", "title": "X", "start": "2030-01-10", "end": "2030-02-30"},
                "end",
                "2030-02-30",
            ),
            ({"action": "update", "id": "{standup}", "start": "9am"}, "start", "9am"),
            (
                {"action": "update", "id": "{standup}_20300109T0900", "end": "half past ten"},
                "end",
                "half past ten",
            ),
        ],
    )
    def test_an_unreadable_time_is_refused_with_the_value_received(
        self, tool: CalendarTool, call: dict[str, str], field: str, value: str
    ) -> None:
        standup = _standup(tool)

        message = tool.refused(
            {key: text.replace("{standup}", standup) for key, text in call.items()}
        )

        corrected = json.loads(message.split(" Send: ", 1)[1])
        assert f'"{value}"' in message.split(" Send: ", 1)[0]
        assert corrected[field] == STAND_INS[field]

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
    def test_list_shows_chronological_occurrences_with_exact_ids(self, tool: CalendarTool) -> None:
        standup = _standup(tool)
        job = _bind_job(tool, standup)
        asyncio.run(
            tool.service.update_occurrence(f"{standup}_20300109T0900", start="2030-01-09T10:00")
        )
        trip = tool.service.create_event(title="Trip", start="2030-01-14", end="2030-01-17").id
        result, text = tool.call(
            {"action": "list", "time_min": "2030-01-06", "time_max": "2030-01-20"}
        )
        assert result["ok"], text
        data = result["data"]
        assert (data["occurrences"], data["complete"], data["window_timezone"]) == (
            5,
            True,
            "Europe/Berlin",
        )
        blocks = [
            dict(line.split(": ", 1) for line in block.splitlines())
            for block in data["content"].split("\n\n")
        ]
        assert [block["id"] for block in blocks] == [
            f"{standup}_20300107T0900",
            f"{standup}_20300109T0900",
            trip,
            f"{standup}_20300114T0900",
            f"{standup}_20300116T0900",
        ]
        assert blocks[1]["start"] == "2030-01-09T10:00+01:00"
        assert blocks[1]["end"] == "2030-01-09T10:15+01:00"
        assert blocks[2]["end"] == "2030-01-17 (last day 2030-01-16)"
        assert all(
            block["series"] == standup and job in block["cron_jobs"]
            for block in blocks
            if block["id"] != trip
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

        assert "window: 2030-01-01T12:00+01:00 to 2030-01-31T12:00+01:00" in text
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

        assert envelope["data"]["occurrences"] == 3
        assert "query" not in text
        assert "Dentist" not in text

    def test_a_long_series_lists_all_occurrences(self, tool: CalendarTool) -> None:
        event = tool.service.create_event(
            title="Daily", start="2030-01-01T08:00", rrule="FREQ=DAILY"
        )
        result, _ = tool.call(
            {"action": "list", "time_min": "2030-01-01", "time_max": "2030-01-31"}
        )
        data = result["data"]
        assert data["occurrences"] == 30
        ids = [line[4:] for line in data["content"].splitlines() if line.startswith("id: ")]
        assert ids[0] == f"{event.id}_20300101T0800"
        assert ids[-1] == f"{event.id}_20300130T0800"
        assert len(set(ids)) == 30

    @pytest.mark.parametrize(
        ("window", "message"),
        [
            (
                {"time_min": "2030-01-10", "time_max": "2030-01-10"},
                'calendar was not run: time_max must come after time_min. Send: {"action":"list",'
                '"time_min":"2030-01-10","time_max":"<2030-01-10 or 2030-01-10T15:00>"}',
            ),
            (
                {"time_min": "next tuesday"},
                'calendar was not run: time_min "next tuesday" is not a date or local date-time. '
                'Send: {"action":"list","time_min":"<2030-01-10 or 2030-01-10T15:00>"}',
            ),
            (
                {"time_min": "2030-01-10", "time_max": "friday"},
                'calendar was not run: time_max "friday" is not a date or local date-time. '
                'Send: {"action":"list","time_min":"2030-01-10",'
                '"time_max":"<2030-01-10 or 2030-01-10T15:00>"}',
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
            "free: 2\nwindow: 2030-01-07T08:00+01:00 to 2030-01-07T18:00+01:00\n"
            "timezone: Europe/Berlin\n"
            "complete: true\n\n"
            "2030-01-07T08:00+01:00 to 2030-01-07T09:00+01:00 (1h)\n"
            "2030-01-07T11:00+01:00 to 2030-01-07T18:00+01:00 (7h)"
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
            "free: 1\nwindow: 2030-01-01T12:00+01:00 to 2030-01-08T12:00+01:00\n"
            "timezone: Europe/Berlin\n"
            "complete: true\n\n"
            "2030-01-03T10:00+01:00 to 2030-01-03T11:00+01:00 (1h)"
        )

    def test_no_free_span_is_named(self, tool: CalendarTool) -> None:
        tool.service.create_event(title="Busy", start="2030-01-07")

        text = tool.succeeded(
            {"action": "find_free_time", "time_min": "2030-01-07", "time_max": "2030-01-08"}
        )

        assert "free: 0" in text
        assert "note: No free span of 1h or more in this window." in text


class TestUpdate:
    @pytest.mark.parametrize("clear_field", [None, "description", "location"])
    @pytest.mark.parametrize("explicit_action", [True, False])
    def test_update_changes_only_the_fields_sent(
        self, tool: CalendarTool, clear_field: str | None, explicit_action: bool
    ) -> None:
        event = tool.service.create_event(
            title="Dentist", start="2030-01-10T15:00", location="Dr. Weiss", description="Card."
        )

        arguments = {"id": event.id, "title": "Dentist checkup"}
        if explicit_action:
            arguments["action"] = "update"
        if clear_field:
            arguments[clear_field] = ""
        text = tool.succeeded(arguments)

        updated = tool.only_event()
        assert updated.title == "Dentist checkup"
        assert (updated.start, updated.end, updated.location, updated.description) == (
            event.start,
            event.end,
            None if clear_field == "location" else event.location,
            None if clear_field == "description" else event.description,
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

    @pytest.mark.parametrize(
        ("start", "rrule", "keys", "changes", "actions"),
        [
            pytest.param(
                "2030-01-07",
                "FREQ=WEEKLY;BYDAY=MO,TU",
                ("2030-01-14", "2030-01-15"),
                {"start": "2030-01-08"},
                ("delete", "delete"),
                id="removed-occurrences",
            ),
            pytest.param(
                "2030-01-07T09:00",
                "FREQ=DAILY;UNTIL=20300109T090000",
                ("2030-01-08T09:00:00", "2030-01-09T09:00:00"),
                {"start": "2030-01-08T09:00"},
                ("update", "update"),
                id="changed-occurrences-at-until",
            ),
            pytest.param(
                "2030-01-07",
                "FREQ=WEEKLY;BYDAY=MO,TU",
                ("2030-01-14", "2030-01-15"),
                {"start": "2030-01-08"},
                ("delete", "update"),
                id="removed-and-changed-occurrences",
            ),
            pytest.param(
                "2030-01-08T09:00",
                "FREQ=WEEKLY;BYDAY=MO,TU",
                ("2030-01-14T09:00:00", "2030-01-15T09:00:00"),
                {"start": "2030-01-07T09:00"},
                ("update", "delete"),
                id="backward-move-collides-with-earlier-kept-key",
            ),
            pytest.param(
                "2030-01-07T09:00",
                "FREQ=DAILY;BYHOUR=9,17",
                ("2030-01-08T09:00:00", "2030-01-08T17:00:00"),
                {"start": "2030-01-07", "rrule": "FREQ=DAILY"},
                ("delete", "update"),
                id="timed-to-all-day-merges-two-times",
            ),
        ],
    )
    def test_series_change_refuses_colliding_exceptions(
        self,
        tool: CalendarTool,
        tmp_path: Path,
        start: str,
        rrule: str,
        keys: tuple[str, str],
        changes: dict[str, str],
        actions: tuple[str, str],
    ) -> None:
        event = tool.service.create_event(title="Series", start=start, rrule=rrule)
        for key, action in zip(keys, actions, strict=True):
            item_id = occurrence_id(event.id, key)
            if action == "delete":
                asyncio.run(tool.service.delete_occurrence(item_id))
            else:
                asyncio.run(tool.service.update_occurrence(item_id, title=key))
        stored = tmp_path / "calendar" / "events.json"
        before = stored.read_bytes()
        notifications: list[None] = []
        tool.service.add_changed_callback(lambda: notifications.append(None))

        message = tool.refused(
            {"action": "update", "id": event.id, "title": "Must not change", **changes}
        )

        assert all(key in message for key in keys)
        assert stored.read_bytes() == before
        assert notifications == []

    @pytest.mark.parametrize("action", ["update", "delete"])
    def test_an_occurrence_id_changes_or_deletes_only_that_occurrence(
        self, tool: CalendarTool, monkeypatch: pytest.MonkeyPatch, action: str
    ) -> None:
        _now(monkeypatch, datetime(2030, 1, 8, 12, tzinfo=UTC))
        standup = _standup(tool)
        target = f"{standup}_20300109T0900"
        changes = {"title": "Long standup", "end": "2030-01-09T10:00"} if action == "update" else {}
        result, text = tool.call({"action": action, "id": target, **changes})
        assert result["ok"], text
        listing = tool.succeeded(
            {"action": "list", "time_min": "2030-01-07", "time_max": "2030-01-15"}
        )
        if action == "update":
            assert result["data"]["id"] == target
            assert result["data"]["end"] == "2030-01-09T10:00+01:00"
            assert f"id: {target}\nseries: {standup}\ntitle: Long standup" in listing
        else:
            assert target not in listing
            assert f"{standup}_20300114T0900" in result["data"]["next"]
        assert f"id: {standup}_20300107T0900" in listing
        assert f"id: {standup}_20300114T0900" in listing
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
            'description, location, rrule or timezone. Send: {"action":"update",'
            f'"id":"{event_id}",'
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
        received = tool.refused(call)
        corrected = json.loads(received.split(" Send: ", 1)[1])
        assert corrected["action"] == call["action"]
        assert corrected["id"] == STAND_INS["id"]
        assert '"action":"search"' in received

    @pytest.mark.parametrize(
        ("item_id", "listing"),
        [
            ("evt_missing", '{"action":"list"}'),
            # An occurrence of an unknown event names the list of all events.
            ("evt_missing_20300108T0900", '{"action":"list"}'),
        ],
    )
    @pytest.mark.parametrize("action", ["update", "delete"])
    def test_unknown_event_names_the_list_call(
        self, tool: CalendarTool, action: str, item_id: str, listing: str
    ) -> None:
        _standup(tool)
        before = [event.to_dict() for event in tool.events()]

        changes = {"title": "Moved"} if action == "update" else {}

        envelope, _text = tool.call({"action": action, "id": item_id, **changes})

        message = envelope["error"]["message"]
        assert envelope["error"]["code"] == "event_not_found"
        assert message.startswith("calendar was not run: ") and listing in message
        assert [event.to_dict() for event in tool.events()] == before

    def test_unknown_occurrence_names_the_list_call_that_shows_the_series(
        self, tool: CalendarTool
    ) -> None:
        standup = _standup(tool)

        envelope, _text = tool.call({"action": "delete", "id": f"{standup}_20300108T0900"})

        message = envelope["error"]["message"]
        assert message.startswith("calendar was not run: ")
        assert f'{{"action":"get","id":"{standup}"}}' in message
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


def _fail_writes(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    def refuse(path: Path, *_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "Zugriff verweigert", str(path))

    monkeypatch.setattr(f"{module}.write_json_document", refuse)


@pytest.mark.parametrize(
    "call",
    [
        {"action": "create", "title": "Dentist", "start": "2030-01-10T15:00"},
        {"action": "update", "id": "{standup}", "title": "Moved"},
        {"action": "update", "id": "{standup}_20300109T0900", "title": "Moved"},
        {"action": "delete", "id": "{standup}"},
        {"action": "delete", "id": "{standup}_20300109T0900"},
    ],
)
def test_a_calendar_that_cannot_be_saved_changes_nothing(
    tool: CalendarTool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, call: dict[str, str]
) -> None:
    standup = _standup(tool)
    before = [event.to_dict() for event in tool.events()]
    _fail_writes(monkeypatch, "core.calendar.service")

    envelope, text = tool.call(
        {key: value.replace("{standup}", standup) for key, value in call.items()}
    )

    assert envelope["error"]["code"] == "calendar_storage_error"
    assert str(tmp_path) not in text and "Zugriff" not in text
    assert [event.to_dict() for event in tool.events()] == before
    monkeypatch.undo()
    reloaded = CalendarService(tmp_path, tz="Europe/Berlin").list_events()
    assert [event.to_dict() for event in reloaded] == before


def test_a_deleted_event_whose_cron_jobs_cannot_be_deleted_says_so(
    tool: CalendarTool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    standup = _standup(tool)
    job = _bind_job(tool, standup)
    _fail_writes(monkeypatch, "core.automation.cron")

    envelope, text = tool.call({"action": "delete", "id": standup})

    assert envelope["error"]["code"] == "cron_storage_error"
    assert standup in text and str(tmp_path) not in text
    assert tool.events() == []
    assert [item.id for item in tool.cron.list_jobs()] == [job]


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


@pytest.mark.parametrize("kind", ["event", "occurrence"])
def test_get_reads_exact_identity_and_full_details(tool: CalendarTool, kind: str) -> None:
    series = _standup(tool)
    changed = f"{series}_20300109T0900"
    removed = f"{series}_20300107T0900"
    description = "Full changed detail " * 100
    asyncio.run(tool.service.update_occurrence(changed, title="Changed", description=description))
    asyncio.run(tool.service.delete_occurrence(removed))
    target = series if kind == "event" else changed
    result, text = tool.call({"action": "get", "id": target})
    assert result["ok"], text
    assert result["data"]["complete"] is True
    content = result["data"]["content"]
    assert content.startswith(f"id: {target}\n")
    if kind == "event":
        assert f"removed_occurrences: {removed}" in content
        assert f"changed_occurrences: {changed}" in content
    else:
        assert f"series: {series}" in content
        assert description.strip() in content
        assert f"{series}_20300114T0900" not in content
        # The tolerated original-start form has the same exact read target.
        assert (
            tool.call({"action": "list", "id": series, "original_start": "2030-01-09T09:00"})[0]
            == result
        )


def test_search_is_global_and_preserves_literal_query_text(tool: CalendarTool) -> None:
    old = tool.service.create_event(title="TBD", start="2000-01-01")
    future = tool.service.create_event(title="Future", start="2050-01-01", rrule="FREQ=YEARLY")
    asyncio.run(tool.service.update_occurrence(occurrence_id(future.id, "2051-01-01"), title="TBD"))
    result, text = tool.call({"action": "search", "query": "TBD"})
    assert result["ok"], text
    assert result["data"]["events"] == 2
    assert f"id: {old.id}" in text and f"id: {future.id}" in text
    assert f"matching_occurrences: {future.id}_20510101" in text
    bounded, _ = tool.call(
        {"action": "list", "query": "TBD", "time_min": "2030-01-01", "time_max": "2031-01-01"}
    )
    assert bounded["data"]["occurrences"] == 0


@pytest.mark.parametrize("bound_names", [("start", "end"), ("time_min", "time_max")])
def test_search_date_aliases_retain_the_window(
    tool: CalendarTool, bound_names: tuple[str, str]
) -> None:
    old = tool.service.create_event(title="Needle", start="2000-01-01")
    current = tool.service.create_event(title="Needle", start="2030-01-01")
    result, text = tool.call(
        {
            "action": "search",
            "query": "Needle",
            bound_names[0]: "2030-01-01",
            bound_names[1]: "2031-01-01",
        }
    )
    assert result["ok"], text
    assert result["data"]["occurrences"] == 1
    assert current.id in text and old.id not in text


def test_named_zone_mutations_and_results_keep_local_intent(tool: CalendarTool) -> None:
    result, text = tool.call(
        {
            "action": "create",
            "title": "New York",
            "start": "2030-03-03T09:00",
            "timezone": "America/New_York",
            "rrule": "FREQ=WEEKLY;COUNT=6",
        }
    )
    assert result["ok"], text
    event = tool.only_event()
    assert (event.start, event.tz_name) == ("2030-03-03T09:00:00", "America/New_York")
    listed, text = tool.call({"action": "list", "time_min": "2030-03-01", "time_max": "2030-04-10"})
    assert listed["data"]["occurrences"] == 6
    assert text.count("timezone: America/New_York") == 6
    assert "2030-03-03T09:00-05:00" in text and "2030-03-10T09:00-04:00" in text
    tool.succeeded({"action": "update", "id": event.id, "start": "2030-03-03T10:00"})
    assert tool.only_event().start == "2030-03-03T10:00:00"
    assert tool.only_event().tz_name == "America/New_York"
