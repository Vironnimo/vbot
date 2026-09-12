"""Auth flow: managed lifecycle behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.providers.auth_flow import DeviceFlowEngine, DeviceFlowSession
from core.providers.token_store import TokenStore
from tests.core.providers.auth_flow_helpers import (
    _oauth_config,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "close"])
async def test_managed_flow_can_stop_before_polling_starts(tmp_path: Path, stop: str) -> None:
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    session = DeviceFlowSession("device", "user", "https://example.test", 900, 5)
    on_complete = AsyncMock()
    with (
        patch.object(engine, "_request_device_session", AsyncMock(return_value=session)),
        patch.object(engine, "_poll_until_complete", AsyncMock()) as poll,
    ):
        result = await engine.connect("provider", "oauth", _oauth_config(), on_complete)
        assert result is session
        assert engine.is_flow_active("provider", "oauth")
        assert not engine.is_flow_active("provider", "oauth", "work")
        if stop == "cancel":
            engine.cancel_flow("provider", "oauth")
            assert not engine.is_flow_active("provider", "oauth")
        await engine.aclose()
        assert not engine.is_flow_active("provider", "oauth")
        poll.assert_not_awaited()
        on_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_replacement_preserves_account_and_drains_cancelled_tasks(
    tmp_path: Path,
) -> None:
    engine = DeviceFlowEngine(TokenStore(tmp_path))
    session = DeviceFlowSession("device", "user", "https://example.test", 900, 5)
    started: asyncio.Queue[str] = asyncio.Queue()
    stopped = []

    async def pending(*args, account_id, **kwargs):
        started.put_nowait(account_id)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(account_id)

    with (
        patch.object(engine, "_request_device_session", AsyncMock(return_value=session)),
        patch.object(engine, "_poll_until_complete", side_effect=pending),
    ):
        await engine.connect("provider", "oauth", _oauth_config(), AsyncMock())
        assert await started.get() == "default"
        await engine.connect("provider", "oauth", _oauth_config(), AsyncMock(), account_id="work")
        assert await started.get() == "work"
        await engine.connect("provider", "oauth", _oauth_config(), AsyncMock())
        assert engine.is_flow_active("provider", "oauth")
        assert engine.is_flow_active("provider", "oauth", "work")
        engine.cancel_flow("provider", "oauth", "work")
        assert not engine.is_flow_active("provider", "oauth", "work")
        assert engine.is_flow_active("provider", "oauth")
        await engine.aclose()
        assert sorted(stopped) == ["default", "work"]
        assert not engine.is_flow_active("provider", "oauth")
        assert not engine.is_flow_active("provider", "oauth", "work")


@pytest.mark.asyncio
async def test_managed_poll_failure_is_observed_and_reports_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from core.providers import auth_flow

    engine = DeviceFlowEngine(TokenStore(tmp_path))
    session = DeviceFlowSession("device", "user", "https://example.test", 900, 5)
    on_complete = AsyncMock()
    recorded = asyncio.Event()

    def record_error(*args, **kwargs):
        recorded.set()

    monkeypatch.setattr(auth_flow._LOGGER, "error", record_error)
    with (
        patch.object(engine, "_request_device_session", AsyncMock(return_value=session)),
        patch.object(
            engine, "_poll_until_complete", AsyncMock(side_effect=RuntimeError("failure"))
        ),
    ):
        await engine.connect("provider", "oauth", _oauth_config(), on_complete)
        await asyncio.wait_for(recorded.wait(), timeout=1)
        await engine.aclose()
    on_complete.assert_awaited_once_with(success=False)
    assert not engine.is_flow_active("provider", "oauth")
