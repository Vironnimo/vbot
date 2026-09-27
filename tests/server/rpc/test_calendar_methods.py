"""Calendar RPCs: the window projection, event mutations and event actions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from core.automation.cron import CronOccurrence
from core.calendar import CalendarService
from server.events import ServerEventBus
from tests.server.rpc_test_support import resource_changes, rpc_error, rpc_result

JsonObject = dict[str, Any]
_CALENDAR_CHANGED = {"kind": "calendar"}
_WEEKLY_STANDUP = {
    "title": "Standup",
    "start": "2026-08-31T09:00:00",
    "rrule": {"freq": "weekly", "by_weekday": ["mo"]},
}


@pytest.fixture()
def state(tmp_path: Path) -> SimpleNamespace:
    cron_service = Mock()
    cron_service.project_occurrences.return_value = []
    return SimpleNamespace(
        runtime=SimpleNamespace(
            calendar_service=CalendarService(tmp_path, tz="Europe/Berlin"),
            cron_service=cron_service,
        ),
        event_bus=ServerEventBus(),
        agent_delete_lock=asyncio.Lock(),
    )


def _configure_actions(state: SimpleNamespace, *, session_exists: bool) -> CalendarService:
    service: CalendarService = state.runtime.calendar_service
    service.actions.configure(Mock(), Mock(), Mock(exists=Mock(return_value=session_exists)))
    return service


@pytest.mark.asyncio
async def test_calendar_window_returns_event_and_cron_layers(state: SimpleNamespace) -> None:
    service = state.runtime.calendar_service
    service.create_event(title="Zahnarzt", start="2026-09-03T15:00:00+02:00")
    state.runtime.cron_service.project_occurrences.return_value = [
        CronOccurrence(
            job_id="job-1",
            name="Check mail",
            fire_at_utc=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
            schedule_type="cron",
        )
    ]

    window = await rpc_result(
        state, "calendar.window", **{"from": "2026-09-01", "to": "2026-09-30"}
    )

    [occurrence] = window["occurrences"]
    assert occurrence["title"] == "Zahnarzt"
    assert len(window["events"]) == 1
    assert window["cron"] == [
        {
            "job_id": "job-1",
            "name": "Check mail",
            "fire_at": "2026-09-03T09:00:00+00:00",
            "schedule_type": "cron",
        }
    ]
    assert window["system_timezone"] == service.system_timezone_name()


@pytest.mark.asyncio
async def test_calendar_event_create_update_delete_roundtrip(state: SimpleNamespace) -> None:
    created = await rpc_result(state, "calendar.create", **_WEEKLY_STANDUP)
    event_id = created["event"]["id"]
    updated = await rpc_result(state, "calendar.update", id=event_id, title="Daily")
    deleted = await rpc_result(state, "calendar.delete", id=event_id)

    assert created["event"]["recurring"] is True
    assert updated["event"]["title"] == "Daily"
    assert deleted == {"id": event_id, "deleted": True}
    assert state.runtime.calendar_service.list_events() == []
    assert resource_changes(state) == [_CALENDAR_CHANGED] * 3


@pytest.mark.asyncio
async def test_calendar_add_exdate_excludes_one_occurrence_additively(
    state: SimpleNamespace,
) -> None:
    created = await rpc_result(state, "calendar.create", **_WEEKLY_STANDUP)
    event_id = created["event"]["id"]

    first = await rpc_result(
        state, "calendar.add_exdate", id=event_id, occurrence_start="2026-09-14T09:00:00"
    )
    # A second exclusion keeps the first rather than replacing it, so clients need
    # no read-modify-write.
    second = await rpc_result(
        state, "calendar.add_exdate", id=event_id, occurrence_start="2026-09-21T09:00:00"
    )

    assert first["event"]["exdates"] == ["2026-09-14T09:00:00"]
    assert second["event"]["exdates"] == ["2026-09-14T09:00:00", "2026-09-21T09:00:00"]


@pytest.mark.asyncio
async def test_calendar_actions_roundtrip(state: SimpleNamespace) -> None:
    service = _configure_actions(state, session_exists=True)
    event = await rpc_result(state, "calendar.create", title="Meeting", start="2026-09-10T15:00")

    created = await rpc_result(
        state,
        "calendar.add_action",
        id=event["event"]["id"],
        when="start - 1h",
        prompt="prepare",
        target="main",
        session="chosen",
    )
    action_id = created["action"]["id"]
    updated = await rpc_result(
        state, "calendar.update_action", id=action_id, session=None, when="end"
    )
    window = await rpc_result(
        state, "calendar.window", **{"from": "2026-09-10", "to": "2026-09-10"}
    )
    deleted = await rpc_result(state, "calendar.delete_action", id=action_id)

    assert event["event"]["recurring"] is False
    assert updated["action"]["session"] is None
    assert updated["action"]["prompt"] == "prepare"
    assert window["actions"][0]["id"] == action_id
    assert window["executions"][0]["action_id"] == action_id
    assert deleted == {"id": action_id, "deleted": True}
    assert service.actions.list_actions() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "code", "named"),
    [
        (
            "calendar.create",
            {"title": "X", "start": "2026-09-03", "rrule": {"freq": "hourly"}},
            "domain_error",
            "rrule.freq",
        ),
        ("calendar.window", {}, "invalid_request", "from"),
        (
            "calendar.window",
            {"from": "2026-09-01", "to": "2026-09-02", "bogus": 1},
            "invalid_request",
            "bogus",
        ),
        ("calendar.update", {"id": "missing", "title": "X"}, "domain_error", "not found"),
        ("calendar.delete", {"id": "{single}", "title": "Y"}, "invalid_request", "title"),
        # Only a recurring event has occurrences to exclude.
        (
            "calendar.add_exdate",
            {"id": "{single}", "occurrence_start": "2026-09-10T15:00:00"},
            "domain_error",
            "",
        ),
        (
            "calendar.add_exdate",
            {"id": "{single}", "occurrence_start": "2026-09-14T09:00:00", "bogus": 1},
            "invalid_request",
            "bogus",
        ),
        # An action's Session must belong to its target.
        (
            "calendar.add_action",
            {
                "id": "{single}",
                "when": "start",
                "prompt": "prepare",
                "target": "main",
                "session": "other",
            },
            "domain_error",
            "",
        ),
    ],
)
async def test_calendar_refusals_change_nothing(
    state: SimpleNamespace, method: str, params: JsonObject, code: str, named: str
) -> None:
    service = _configure_actions(state, session_exists=False)
    single = service.create_event(title="X", start="2026-09-10T15:00:00")
    before = [event.to_dict() for event in service.list_events()]
    params = {key: single.id if value == "{single}" else value for key, value in params.items()}

    error = await rpc_error(state, method, **params)

    assert error["code"] == code
    assert named in error["message"]
    assert [event.to_dict() for event in service.list_events()] == before
    assert service.actions.list_actions() == []
    assert resource_changes(state) == []
