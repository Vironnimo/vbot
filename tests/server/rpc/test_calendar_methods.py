"""Calendar RPCs: the window projection and event mutations."""

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
    "end": "2026-08-31T09:30:00",
    "description": "Status round.",
    "location": "Hall",
    "rrule": "FREQ=WEEKLY;BYDAY=MO",
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
    assert occurrence == {
        "id": occurrence["event_id"],
        "event_id": occurrence["event_id"],
        "title": "Zahnarzt",
        "description": None,
        "location": None,
        "all_day": False,
        "recurring": False,
        "start": "2026-09-03T15:00:00",
        "end": "2026-09-03T16:00:00",
        "start_utc": "2026-09-03T13:00:00+00:00",
        "end_utc": "2026-09-03T14:00:00+00:00",
        "original_start": None,
        "overridden": False,
    }
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
    updated = await rpc_result(
        state, "calendar.update", id=event_id, title="Daily", location=None, rrule="FREQ=DAILY"
    )
    deleted = await rpc_result(state, "calendar.delete", id=event_id)

    event = created["event"]
    assert (event["start"], event["end"], event["tz_name"]) == (
        "2026-08-31T09:00:00",
        "2026-08-31T09:30:00",
        "Europe/Berlin",
    )
    assert (event["description"], event["location"], event["rrule"]) == (
        "Status round.",
        "Hall",
        "FREQ=WEEKLY;BYDAY=MO",
    )
    assert (event["recurring"], event["all_day"], event["exdates"], event["overrides"]) == (
        True,
        False,
        [],
        {},
    )
    changed = updated["event"]
    assert (changed["title"], changed["location"], changed["rrule"]) == (
        "Daily",
        None,
        "FREQ=DAILY",
    )
    assert deleted == {"id": event_id, "deleted": True}
    assert state.runtime.calendar_service.list_events() == []
    assert resource_changes(state) == [_CALENDAR_CHANGED] * 3


@pytest.mark.asyncio
async def test_occurrence_id_changes_or_removes_one_occurrence(state: SimpleNamespace) -> None:
    created = await rpc_result(state, "calendar.create", **_WEEKLY_STANDUP)
    event_id = created["event"]["id"]

    moved = await rpc_result(
        state,
        "calendar.update",
        id=f"{event_id}_20260907T0900",
        title="Planning",
        start="2026-09-08T14:00:00",
    )
    removed = await rpc_result(state, "calendar.delete", id=f"{event_id}_20260914T0900")

    occurrence = moved["occurrence"]
    assert (occurrence["id"], occurrence["title"], occurrence["start"], occurrence["end"]) == (
        f"{event_id}_20260907T0900",
        "Planning",
        "2026-09-08T14:00:00",
        "2026-09-08T14:30:00",
    )
    assert (occurrence["original_start"], occurrence["overridden"]) == (
        "2026-09-07T09:00:00",
        True,
    )
    assert moved["event"]["overrides"] == {
        "2026-09-07T09:00:00": {
            "title": "Planning",
            "start": "2026-09-08T14:00:00",
            "end": "2026-09-08T14:30:00",
        }
    }
    assert removed["deleted"] is True
    assert removed["event"]["exdates"] == ["2026-09-14T09:00:00"]
    window = await rpc_result(
        state, "calendar.window", **{"from": "2026-08-31", "to": "2026-09-21"}
    )
    assert [item["start"] for item in window["occurrences"]] == [
        "2026-08-31T09:00:00",
        "2026-09-08T14:00:00",
        "2026-09-21T09:00:00",
    ]
    assert resource_changes(state) == [_CALENDAR_CHANGED] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "code", "named"),
    [
        (
            "calendar.create",
            {"title": "X", "start": "2026-09-03", "rrule": "FREQ=HOURLY"},
            "domain_error",
            "FREQ must be",
        ),
        (
            "calendar.create",
            {"title": "X", "start": "2026-09-03", "description": 5},
            "invalid_request",
            "params.description",
        ),
        ("calendar.window", {}, "invalid_request", "from"),
        (
            "calendar.window",
            {"from": "2026-09-01", "to": "2026-09-02", "bogus": 1},
            "invalid_request",
            "bogus",
        ),
        ("calendar.update", {"id": "missing", "title": "X"}, "domain_error", "not found"),
        (
            "calendar.update",
            {"id": "{series}_20260914T0900", "rrule": "FREQ=DAILY"},
            "invalid_request",
            "params.rrule",
        ),
        (
            "calendar.update",
            {"id": "{series}_20260915T0900", "title": "Y"},
            "domain_error",
            "not found",
        ),
        ("calendar.delete", {"id": "{series}", "title": "Y"}, "invalid_request", "title"),
        ("calendar.delete", {"id": "{series}_20260915T0900"}, "domain_error", "not found"),
        (
            "calendar.add_exdate",
            {"id": "{series}", "occurrence_start": "2026-09-14T09:00:00"},
            "method_not_found",
            "",
        ),
    ],
)
async def test_calendar_refusals_change_nothing(
    state: SimpleNamespace, method: str, params: JsonObject, code: str, named: str
) -> None:
    service: CalendarService = state.runtime.calendar_service
    series = service.create_event(title="X", start="2026-09-07T09:00:00", rrule="FREQ=WEEKLY")
    before = [event.to_dict() for event in service.list_events()]
    params = {
        key: value.replace("{series}", series.id) if isinstance(value, str) else value
        for key, value in params.items()
    }

    error = await rpc_error(state, method, **params)

    assert error["code"] == code
    assert named in error["message"]
    assert [event.to_dict() for event in service.list_events()] == before
    assert resource_changes(state) == []
