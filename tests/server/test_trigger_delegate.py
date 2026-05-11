"""Tests for the automation trigger RPC delegate."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.chat import Run, RunStatus
from server.delegates import dispatch_rpc


def make_state(run: Run) -> SimpleNamespace:
    trigger_service = SimpleNamespace(trigger_run=AsyncMock(return_value=run))
    runtime = SimpleNamespace(trigger_service=trigger_service)
    return SimpleNamespace(runtime=runtime)


@pytest.mark.asyncio
async def test_automation_trigger_requires_agent_id() -> None:
    # Arrange
    state = make_state(Run(run_id="run-one", agent_id="coder", session_id="session-one"))

    # Act
    response = await dispatch_rpc(
        state,
        {"method": "automation.trigger", "params": {"message": "Start work"}},
    )

    # Assert
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_automation_trigger_requires_message() -> None:
    # Arrange
    state = make_state(Run(run_id="run-one", agent_id="coder", session_id="session-one"))

    # Act
    response = await dispatch_rpc(
        state,
        {"method": "automation.trigger", "params": {"agent_id": "coder"}},
    )

    # Assert
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_automation_trigger_returns_started_run_payload() -> None:
    # Arrange
    run = Run(run_id="run-one", agent_id="coder", session_id="session-one")
    state = make_state(run)

    # Act
    response = await dispatch_rpc(
        state,
        {
            "method": "automation.trigger",
            "params": {
                "agent_id": "coder",
                "message": "Start work",
                "session_id": "session-one",
            },
        },
    )

    # Assert
    assert response == {
        "ok": True,
        "result": {
            "run_id": "run-one",
            "agent_id": "coder",
            "session_id": "session-one",
            "status": RunStatus.RUNNING.value,
        },
    }
    state.runtime.trigger_service.trigger_run.assert_awaited_once_with(
        "coder",
        "Start work",
        session_id="session-one",
    )


@pytest.mark.asyncio
async def test_automation_trigger_allows_omitted_session_id() -> None:
    # Arrange
    run = Run(run_id="run-one", agent_id="coder", session_id="created-session")
    state = make_state(run)

    # Act
    response = await dispatch_rpc(
        state,
        {"method": "automation.trigger", "params": {"agent_id": "coder", "message": "Start"}},
    )

    # Assert
    assert response["ok"] is True
    assert response["result"]["session_id"] == "created-session"
    state.runtime.trigger_service.trigger_run.assert_awaited_once_with(
        "coder",
        "Start",
        session_id=None,
    )
