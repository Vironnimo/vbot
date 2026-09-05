"""Tests for calendar RPC handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

import pytest

from core.calendar import CalendarService
from server.rpc.methods import dispatch_rpc


@pytest.fixture()
def state(tmp_path: Any) -> Mock:
    runtime = Mock()
    runtime.calendar_service = CalendarService(tmp_path, tz="Europe/Berlin")
    runtime.cron_service = Mock()
    runtime.cron_service.project_occurrences.return_value = []
    return Mock(runtime=runtime)


@pytest.mark.asyncio
async def test_calendar_window_returns_layers(state: Mock) -> None:
    service = state.runtime.calendar_service
    service.create_event(title="Zahnarzt", start="2026-09-03T15:00:00+02:00")
    response = await dispatch_rpc(
        state, {"method": "calendar.window", "params": {"from": "2026-09-01", "to": "2026-09-30"}}
    )
    assert response["ok"] is True
    data = response["result"]
    assert len(data["occurrences"]) == 1
    assert data["occurrences"][0]["title"] == "Zahnarzt"
    assert len(data["events"]) == 1
    assert data["cron"] == []
    assert data["system_timezone"] == service.system_timezone_name()


@pytest.mark.asyncio
async def test_calendar_window_includes_cron_layer(state: Mock) -> None:
    from core.automation.cron import CronOccurrence

    state.runtime.cron_service.project_occurrences.return_value = [
        CronOccurrence(
            job_id="job-1",
            name="Check mail",
            fire_at_utc=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
            schedule_type="cron",
        )
    ]
    response = await dispatch_rpc(
        state, {"method": "calendar.window", "params": {"from": "2026-09-01", "to": "2026-09-30"}}
    )
    assert response["result"]["cron"] == [
        {
            "job_id": "job-1",
            "name": "Check mail",
            "fire_at": "2026-09-03T09:00:00+00:00",
            "schedule_type": "cron",
        }
    ]


@pytest.mark.asyncio
async def test_calendar_create_update_delete_roundtrip(state: Mock) -> None:
    created = await dispatch_rpc(
        state,
        {
            "method": "calendar.create",
            "params": {
                "title": "Standup",
                "start": "2026-08-31T09:00:00",
                "rrule": {"freq": "weekly", "by_weekday": ["mo"]},
            },
        },
    )
    assert created["result"]["event"]["recurring"] is True
    event_id = created["result"]["event"]["id"]

    updated = await dispatch_rpc(
        state, {"method": "calendar.update", "params": {"id": event_id, "title": "Daily"}}
    )
    assert updated["result"]["event"]["title"] == "Daily"

    deleted = await dispatch_rpc(state, {"method": "calendar.delete", "params": {"id": event_id}})
    assert deleted["result"]["deleted"] is True

    assert state.runtime.calendar_service.list_events() == []


@pytest.mark.asyncio
async def test_calendar_create_rejects_invalid_rrule(state: Mock) -> None:
    response = await dispatch_rpc(
        state,
        {
            "method": "calendar.create",
            "params": {"title": "X", "start": "2026-09-03", "rrule": {"freq": "hourly"}},
        },
    )
    assert response["error"]["code"] == "domain_error"
    assert "rrule.freq" in response["error"]["message"]


@pytest.mark.asyncio
async def test_calendar_window_requires_bounds(state: Mock) -> None:
    response = await dispatch_rpc(state, {"method": "calendar.window", "params": {}})
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_calendar_window_rejects_unknown_fields(state: Mock) -> None:
    response = await dispatch_rpc(
        state,
        {
            "method": "calendar.window",
            "params": {"from": "2026-09-01", "to": "2026-09-02", "bogus": 1},
        },
    )
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_calendar_update_missing_event_maps_domain_error(state: Mock) -> None:
    response = await dispatch_rpc(
        state, {"method": "calendar.update", "params": {"id": "missing", "title": "X"}}
    )
    assert response["error"]["code"] == "domain_error"
    assert "not found" in response["error"]["message"]


@pytest.mark.asyncio
async def test_calendar_delete_rejects_unknown_fields(state: Mock) -> None:
    response = await dispatch_rpc(
        state, {"method": "calendar.delete", "params": {"id": "x", "title": "Y"}}
    )
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_calendar_add_exdate_excludes_one_occurrence_additively(state: Mock) -> None:
    created = await dispatch_rpc(
        state,
        {
            "method": "calendar.create",
            "params": {
                "title": "Standup",
                "start": "2026-08-31T09:00:00",
                "rrule": {"freq": "weekly", "by_weekday": ["mo"]},
            },
        },
    )
    event_id = created["result"]["event"]["id"]

    first = await dispatch_rpc(
        state,
        {
            "method": "calendar.add_exdate",
            "params": {"id": event_id, "occurrence_start": "2026-09-14T09:00:00"},
        },
    )
    assert first["ok"] is True
    assert first["result"]["event"]["exdates"] == ["2026-09-14T09:00:00"]

    # Additive semantics: a second exclusion keeps the first rather than
    # replacing it (no read-modify-write race on the client side).
    second = await dispatch_rpc(
        state,
        {
            "method": "calendar.add_exdate",
            "params": {"id": event_id, "occurrence_start": "2026-09-21T09:00:00"},
        },
    )
    assert second["result"]["event"]["exdates"] == [
        "2026-09-14T09:00:00",
        "2026-09-21T09:00:00",
    ]


@pytest.mark.asyncio
async def test_calendar_add_exdate_rejects_single_event(state: Mock) -> None:
    created = await dispatch_rpc(
        state,
        {"method": "calendar.create", "params": {"title": "X", "start": "2026-09-10T15:00:00"}},
    )
    event_id = created["result"]["event"]["id"]
    response = await dispatch_rpc(
        state,
        {
            "method": "calendar.add_exdate",
            "params": {"id": event_id, "occurrence_start": "2026-09-10T15:00:00"},
        },
    )
    assert response["error"]["code"] == "domain_error"


@pytest.mark.asyncio
async def test_calendar_add_exdate_rejects_unknown_fields(state: Mock) -> None:
    response = await dispatch_rpc(
        state,
        {
            "method": "calendar.add_exdate",
            "params": {"id": "x", "occurrence_start": "2026-09-14T09:00:00", "bogus": 1},
        },
    )
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_calendar_actions_roundtrip(state: Mock) -> None:
    import asyncio

    state.agent_delete_lock = asyncio.Lock()
    service = state.runtime.calendar_service
    service.actions.configure(Mock(), Mock(), Mock(exists=Mock(return_value=True)))
    event = service.create_event(title="Meeting", start="2026-09-10T15:00")
    created = await dispatch_rpc(
        state,
        {
            "method": "calendar.add_action",
            "params": {
                "id": event.id,
                "when": "start - 1h",
                "prompt": "prepare",
                "target": "main",
                "session": "chosen",
            },
        },
    )
    assert created["ok"] is True
    action_id = created["result"]["action"]["id"]
    updated = await dispatch_rpc(
        state,
        {
            "method": "calendar.update_action",
            "params": {
                "id": action_id,
                "session": None,
                "when": "end",
            },
        },
    )
    assert updated["result"]["action"]["session"] is None
    assert updated["result"]["action"]["prompt"] == "prepare"
    listed = await dispatch_rpc(
        state,
        {
            "method": "calendar.window",
            "params": {
                "from": "2026-09-10",
                "to": "2026-09-10",
            },
        },
    )
    assert listed["result"]["actions"][0]["id"] == action_id
    assert listed["result"]["executions"][0]["action_id"] == action_id
    deleted = await dispatch_rpc(
        state, {"method": "calendar.delete_action", "params": {"id": action_id}}
    )
    assert deleted["result"]["deleted"] is True
    assert service.actions.list_actions() == []


@pytest.mark.asyncio
async def test_calendar_action_rejects_session_of_another_target(state: Mock) -> None:
    import asyncio

    state.agent_delete_lock = asyncio.Lock()
    service = state.runtime.calendar_service
    service.actions.configure(Mock(), Mock(), Mock(exists=Mock(return_value=False)))
    event = service.create_event(title="Meeting", start="2026-09-10T15:00")
    result = await dispatch_rpc(
        state,
        {
            "method": "calendar.add_action",
            "params": {
                "id": event.id,
                "when": "start",
                "prompt": "prepare",
                "target": "main",
                "session": "other",
            },
        },
    )
    assert result["error"]["code"] == "domain_error"
    assert service.actions.list_actions() == []
