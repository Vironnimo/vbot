"""Tests for the calendar tool handler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.calendar import CalendarService
from core.tools.calendar import CALENDAR_TOOL_NAME, register_calendar_tool
from core.tools.tools import ToolContext, ToolRegistry, tool_failure

BERLIN = ZoneInfo("Europe/Berlin")


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        agent_id="agent-one",
        session_id="session-one",
        run_id="run-one",
        tool_call_id="call-one",
        tool_name=CALENDAR_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        project_id=None,
    )


def _registry(tmp_path: Path) -> tuple[ToolRegistry, CalendarService]:
    service = CalendarService(tmp_path, tz="Europe/Berlin")
    registry = ToolRegistry()
    register_calendar_tool(registry, service)
    return registry, service


async def _dispatch(registry: ToolRegistry, tmp_path: Path, arguments: dict[str, Any]) -> Any:
    try:
        return await registry.dispatch(_context(tmp_path), arguments, [CALENDAR_TOOL_NAME])
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)


def _run(registry: ToolRegistry, tmp_path: Path, arguments: dict[str, Any]) -> Any:
    return asyncio.run(_dispatch(registry, tmp_path, arguments))


class TestListAction:
    def test_list_defaults_to_the_current_month(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        window_start, window_end = service.resolve_when("this month")
        inside = (window_start.astimezone(BERLIN) + timedelta(days=2)).date().isoformat()
        outside = (window_end.astimezone(BERLIN) + timedelta(days=10)).date().isoformat()
        service.create_event(title="In month", start=f"{inside}T10:00:00")
        service.create_event(title="Far away", start=f"{outside}T10:00:00")

        result = _run(registry, tmp_path, {"action": "list"})

        assert result["ok"] is True
        titles = [item["title"] for item in result["data"]["occurrences"]]
        assert titles == ["In month"]
        assert result["data"]["system_timezone"] == "Europe/Berlin"

    def test_list_accepts_when_expressions(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        window_start, _window_end = service.resolve_when("next week")
        monday = window_start.astimezone(BERLIN).date().isoformat()
        service.create_event(
            title="Weekly",
            start=f"{monday}T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )

        result = _run(registry, tmp_path, {"action": "list", "when": "next week"})

        assert result["ok"] is True
        starts = [item["start"] for item in result["data"]["occurrences"]]
        assert starts == [f"{monday}T09:00:00"]

    def test_list_occurrence_start_matches_exdate_form(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(
            title="Weekly",
            start="2026-09-07T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )

        result = _run(registry, tmp_path, {"action": "list", "when": "2026-09-14..2026-09-14"})

        occurrence = result["data"]["occurrences"][0]
        assert occurrence["event_id"] == event.id
        assert occurrence["start"] == "2026-09-14T09:00:00"
        assert occurrence["end"] == "2026-09-14T10:00:00"
        assert occurrence["recurring"] is True

    def test_list_events_carry_agent_facing_shapes(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        window_start, _window_end = service.resolve_when("next week")
        monday = window_start.astimezone(BERLIN).date().isoformat()
        trip_day = (window_start.astimezone(BERLIN) + timedelta(days=1)).date().isoformat()
        service.create_event(
            title="Weekly",
            start=f"{monday}T09:00:00",
            duration_minutes=30,
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )
        service.create_event(title="Trip", start=trip_day, duration_days=3)

        result = _run(registry, tmp_path, {"action": "list", "when": "next week"})

        by_title = {item["title"]: item for item in result["data"]["events"]}
        assert by_title["Weekly"]["start"] == f"{monday}T09:00:00"
        assert by_title["Weekly"]["end"] == f"{monday}T09:30:00"
        assert by_title["Weekly"]["duration"] == 30
        assert by_title["Trip"]["start"] == trip_day
        assert by_title["Trip"]["duration"] == 3
        assert by_title["Trip"]["all_day"] is True

    def test_list_rejects_unknown_when(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(registry, tmp_path, {"action": "list", "when": "someday"})

        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_arguments"
        assert "cannot parse when" in result["error"]["message"]


class TestCreateAction:
    def test_create_timed_event_with_defaults(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(
            registry,
            tmp_path,
            {"action": "create", "title": "Dentist", "start": "2026-09-10T15:00"},
        )

        assert result["ok"] is True
        event = result["data"]["event"]
        assert event["start"] == "2026-09-10T15:00:00"
        assert event["end"] == "2026-09-10T16:00:00"
        assert event["duration"] == 60
        assert event["all_day"] is False

    def test_create_all_day_event_with_duration_days(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)

        result = _run(
            registry,
            tmp_path,
            {"action": "create", "title": "Trip", "start": "2026-09-14", "duration": 3},
        )

        assert result["ok"] is True
        event = result["data"]["event"]
        assert event["start"] == "2026-09-14"
        assert event["duration"] == 3
        stored = service.get_event(event["id"])
        assert stored.duration_days == 3
        assert stored.duration_minutes is None

    def test_create_repeating_event_anchors_in_server_zone(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)

        result = _run(
            registry,
            tmp_path,
            {
                "action": "create",
                "title": "Standup",
                "start": "2026-08-31T09:00:00",
                "rrule": {"freq": "weekly", "by_weekday": ["mo"]},
            },
        )

        assert result["ok"] is True
        event = result["data"]["event"]
        assert event["recurring"] is True
        stored = service.get_event(event["id"])
        assert stored.tz_name == "Europe/Berlin"
        assert stored.start_local == "2026-08-31T09:00:00"

    def test_create_rejects_missing_title(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(registry, tmp_path, {"action": "create", "start": "2026-09-10T15:00"})

        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_arguments"
        assert "title" in result["error"]["message"]

    def test_create_rejects_action_foreign_fields(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(
            registry,
            tmp_path,
            {"action": "create", "title": "X", "start": "2026-09-10", "id": "bogus"},
        )

        assert result["ok"] is False
        assert "does not accept: id" in result["error"]["message"]
        assert 'Use {"action":"create"' in result["error"]["message"]

    def test_create_rejects_rrule_on_single_event_semantics(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(
            registry,
            tmp_path,
            {"action": "create", "title": "X", "start": "2026-09-10T15:00", "rrule": None},
        )

        assert result["ok"] is False
        assert "rrule" in result["error"]["message"]


class TestUpdateAction:
    def test_update_changes_only_provided_fields(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(
            title="Standup",
            start="2026-09-07T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )

        result = _run(registry, tmp_path, {"action": "update", "id": event.id, "title": "Daily"})

        assert result["ok"] is True
        updated = service.get_event(event.id)
        assert updated.title == "Daily"
        assert updated.duration_minutes == 60
        assert updated.rrule is not None

    def test_update_duration_maps_to_timed_minutes(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(title="X", start="2026-09-10T15:00:00")

        result = _run(registry, tmp_path, {"action": "update", "id": event.id, "duration": 90})

        assert result["ok"] is True
        assert service.get_event(event.id).duration_minutes == 90

    def test_update_duration_maps_to_days_for_all_day_event(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(title="Trip", start="2026-09-14")

        result = _run(registry, tmp_path, {"action": "update", "id": event.id, "duration": 5})

        assert result["ok"] is True
        assert service.get_event(event.id).duration_days == 5

    def test_update_start_switches_all_day_event_to_timed(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(title="Trip", start="2026-09-14", duration_days=3)

        result = _run(
            registry,
            tmp_path,
            {"action": "update", "id": event.id, "start": "2026-09-14T15:00", "duration": 60},
        )

        assert result["ok"] is True
        updated = service.get_event(event.id)
        assert updated.all_day is False
        assert updated.start_utc == "2026-09-14T13:00:00+00:00"
        assert updated.duration_minutes == 60
        assert updated.duration_days is None

    def test_update_null_rrule_stops_recurrence(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(
            title="Standup",
            start="2026-09-07T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )

        result = _run(registry, tmp_path, {"action": "update", "id": event.id, "rrule": None})

        assert result["ok"] is True
        updated = service.get_event(event.id)
        assert updated.rrule is None
        assert updated.start_utc == "2026-09-07T07:00:00+00:00"

    def test_update_requires_at_least_one_field(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(title="X", start="2026-09-10T15:00:00")

        result = _run(registry, tmp_path, {"action": "update", "id": event.id})

        assert result["ok"] is False
        assert "at least one field" in result["error"]["message"]

    def test_update_missing_event_returns_not_found(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(registry, tmp_path, {"action": "update", "id": "missing", "title": "X"})

        assert result["ok"] is False
        assert result["error"]["code"] == "event_not_found"
        assert "list" in result["error"]["message"]


class TestDeleteAction:
    def test_delete_removes_whole_event(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(title="X", start="2026-09-10T15:00:00")

        result = _run(registry, tmp_path, {"action": "delete", "id": event.id})

        assert result["ok"] is True
        assert result["data"]["deleted"] is True
        assert service.list_events() == []

    def test_delete_with_occurrence_start_excludes_one_occurrence(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(
            title="Standup",
            start="2026-09-07T09:00:00",
            rrule={"freq": "weekly", "by_weekday": ["mo"]},
        )

        result = _run(
            registry,
            tmp_path,
            {"action": "delete", "id": event.id, "start": "2026-09-14T09:00:00"},
        )

        assert result["ok"] is True
        assert result["data"]["excluded_occurrence"] == "2026-09-14T09:00:00"
        stored = service.get_event(event.id)
        assert stored.exdates == ["2026-09-14T09:00:00"]

    def test_delete_occurrence_on_single_event_is_rejected(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        event = service.create_event(title="X", start="2026-09-10T15:00:00")

        result = _run(
            registry, tmp_path, {"action": "delete", "id": event.id, "start": "2026-09-10T15:00:00"}
        )

        assert result["ok"] is False
        assert "Single events" in result["error"]["message"]
        assert service.get_event(event.id) is not None


class TestFindFreeAction:
    def test_find_free_returns_slots_in_server_local_time(self, tmp_path: Path) -> None:
        registry, service = _registry(tmp_path)
        service.create_event(title="Block", start="2026-09-03T15:00:00+02:00")

        result = _run(
            registry,
            tmp_path,
            {"action": "find_free", "when": "2026-09-03", "duration": 60},
        )

        assert result["ok"] is True
        slots = result["data"]["slots"]
        assert len(slots) >= 1
        blocked_start = datetime(2026, 9, 3, 13, 0, tzinfo=UTC)
        blocked_end = blocked_start + timedelta(minutes=60)
        for slot in slots:
            start = datetime.fromisoformat(slot["start"]).replace(tzinfo=BERLIN)
            end = start + timedelta(minutes=60)
            overlaps = start < blocked_end and end > blocked_start
            assert not overlaps

    def test_find_free_defaults_to_next_seven_days(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(registry, tmp_path, {"action": "find_free"})

        assert result["ok"] is True
        assert result["data"]["slots"], "an empty calendar must offer slots"

    def test_find_free_rejects_bad_duration(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(
            registry, tmp_path, {"action": "find_free", "when": "this week", "duration": 0}
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_arguments"


class TestActionValidation:
    def test_rejects_unknown_action(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(registry, tmp_path, {"action": "bogus"})

        assert result["ok"] is False
        assert "not one of" in result["error"]["message"]

    def test_rejects_missing_action(self, tmp_path: Path) -> None:
        registry, _service = _registry(tmp_path)

        result = _run(registry, tmp_path, {})

        assert result["ok"] is False
        assert "action" in result["error"]["message"]
