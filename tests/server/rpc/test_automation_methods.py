"""Tests for cron (automation) RPC handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from zoneinfo import ZoneInfoNotFoundError

import pytest

from core.automation.cron import CronServiceError
from server.rpc.methods import dispatch_rpc


def _state_with_cron_service(
    cron_service: Any,
    *,
    resolver: object | None = None,
) -> SimpleNamespace:
    # The cron RPC validates the target through the agent resolver (the same seam
    # every run path uses), so the stub state carries one. A bare resolver Mock
    # resolves any target; tests that exercise the rejection path inject a side
    # effect.
    agent_resolver = resolver if resolver is not None else Mock()
    if isinstance(agent_resolver, Mock):
        agent_resolver.resolve_agent.return_value = SimpleNamespace(id="main")
    return SimpleNamespace(
        runtime=SimpleNamespace(cron_service=cron_service, agent_resolver=agent_resolver)
    )


@pytest.mark.asyncio
async def test_cron_create_happy_path() -> None:
    cron_service = Mock()
    cron_service.create_job.return_value = SimpleNamespace(id="job-123")
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "main",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
                "timezone": "UTC",
                "session_id": "session-1",
            },
        },
    )

    assert response == {"ok": True, "result": {"id": "job-123"}}
    cron_service.create_job.assert_called_once_with(
        agent_id="main",
        prompt="Run status check",
        schedule_type="cron",
        cron_expression="*/5 * * * *",
        run_at=None,
        timezone="UTC",
        session_id="session-1",
        project_id=None,
    )


@pytest.mark.asyncio
async def test_cron_create_parses_project_qualified_target() -> None:
    cron_service = Mock()
    cron_service.create_job.return_value = SimpleNamespace(id="job-123")
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "builder@vbot",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        },
    )

    assert response == {"ok": True, "result": {"id": "job-123"}}
    # The address form is split once at the edge: agent_id + project_id, never an
    # "@" string in agent_id. The resolver validates the project target.
    state.runtime.agent_resolver.resolve_agent.assert_called_once_with("vbot", "builder")
    cron_service.create_job.assert_called_once_with(
        agent_id="builder",
        prompt="Run status check",
        schedule_type="cron",
        cron_expression="*/5 * * * *",
        run_at=None,
        timezone=None,
        session_id=None,
        project_id="vbot",
    )


@pytest.mark.asyncio
async def test_cron_list_happy_path_includes_server_side_next_fire_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> FrozenDateTime:
            base = cls(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
            if tz is None:
                return base
            return cast(FrozenDateTime, base.astimezone(tz))

    monkeypatch.setattr("server.rpc.automation_methods.datetime", FrozenDateTime)

    def missing_zoneinfo(_timezone_name: str) -> Any:
        raise ZoneInfoNotFoundError("timezone data unavailable")

    monkeypatch.setattr("server.rpc.automation_methods.ZoneInfo", missing_zoneinfo)

    job = SimpleNamespace(
        id="job-1",
        agent_id="builder",
        project_id="vbot",
        prompt="Check reports",
        schedule_type="cron",
        cron_expression="*/5 * * * *",
        run_at=None,
        timezone="UTC",
        session_id="session-1",
        status="active",
        last_fired_at="2026-05-14T09:55:00+00:00",
        created_at="2026-05-14T09:00:00+00:00",
    )
    cron_service = Mock()
    cron_service.list_jobs.return_value = [job]
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(state, {"method": "cron.list", "params": {}})

    assert response == {
        "ok": True,
        "result": {
            "jobs": [
                {
                    "id": "job-1",
                    "agent_id": "builder",
                    "project_id": "vbot",
                    "target": "builder@vbot",
                    "prompt": "Check reports",
                    "schedule_type": "cron",
                    "cron_expression": "*/5 * * * *",
                    "run_at": None,
                    "timezone": "UTC",
                    "session_id": "session-1",
                    "status": "active",
                    "last_fired_at": "2026-05-14T09:55:00+00:00",
                    "next_fire_at": "2026-05-14T10:05:00+00:00",
                    "created_at": "2026-05-14T09:00:00+00:00",
                }
            ]
        },
    }
    cron_service.list_jobs.assert_called_once_with()


@pytest.mark.asyncio
async def test_cron_update_happy_path() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.update",
            "params": {
                "id": "job-1",
                "prompt": "Updated prompt",
                "status": "paused",
            },
        },
    )

    assert response == {"ok": True, "result": {"ok": True}}
    cron_service.update_job.assert_called_once_with(
        "job-1",
        prompt="Updated prompt",
        status="paused",
    )


@pytest.mark.asyncio
async def test_cron_update_validates_agent_when_agent_id_is_present() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.update",
            "params": {
                "id": "job-1",
                "agent_id": "main",
            },
        },
    )

    assert response == {"ok": True, "result": {"ok": True}}
    state.runtime.agent_resolver.resolve_agent.assert_called_once_with(None, "main")
    cron_service.update_job.assert_called_once_with("job-1", agent_id="main", project_id=None)


@pytest.mark.asyncio
async def test_cron_delete_happy_path() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.delete",
            "params": {"id": "job-1"},
        },
    )

    assert response == {"ok": True, "result": {"ok": True}}
    cron_service.delete_job.assert_called_once_with("job-1")


@pytest.mark.asyncio
async def test_cron_enable_happy_path() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.enable",
            "params": {"id": "job-1"},
        },
    )

    assert response == {"ok": True, "result": {"ok": True}}
    cron_service.enable_job.assert_called_once_with("job-1")


@pytest.mark.asyncio
async def test_cron_disable_happy_path() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.disable",
            "params": {"id": "job-1"},
        },
    )

    assert response == {"ok": True, "result": {"ok": True}}
    cron_service.disable_job.assert_called_once_with("job-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "cron.create",
            {
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        ),
        ("cron.list", {"extra": True}),
        ("cron.update", {"prompt": "missing id"}),
        ("cron.delete", {}),
        ("cron.enable", {}),
        ("cron.disable", {}),
    ],
)
async def test_cron_methods_reject_invalid_params(method: str, params: dict[str, Any]) -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_cron_create_wraps_expected_domain_errors() -> None:
    cron_service = Mock()
    cron_service.create_job.side_effect = CronServiceError("bad schedule")
    state = _state_with_cron_service(cron_service)

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "main",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        },
    )

    assert response == {
        "ok": False,
        "error": {"code": "domain_error", "message": "bad schedule"},
    }


@pytest.mark.asyncio
async def test_cron_create_rejects_unknown_agent() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)
    state.runtime.agent_resolver.resolve_agent.side_effect = KeyError("missing")

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "missing",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        },
    )

    assert response == {
        "ok": False,
        "error": {"code": "domain_error", "message": "Unknown cron target: missing"},
    }
    cron_service.create_job.assert_not_called()


@pytest.mark.asyncio
async def test_cron_create_rejects_unknown_project_target() -> None:
    cron_service = Mock()
    state = _state_with_cron_service(cron_service)
    state.runtime.agent_resolver.resolve_agent.side_effect = KeyError("not on team")

    response = await dispatch_rpc(
        state,
        {
            "method": "cron.create",
            "params": {
                "agent_id": "ghost@vbot",
                "prompt": "Run status check",
                "schedule_type": "cron",
                "cron_expression": "*/5 * * * *",
            },
        },
    )

    assert response == {
        "ok": False,
        "error": {"code": "domain_error", "message": "Unknown cron target: ghost@vbot"},
    }
    cron_service.create_job.assert_not_called()
