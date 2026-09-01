"""Tests for the keep-awake live-settings delta on ``settings.update``."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.rpc.methods import dispatch_rpc
from tests.server.test_rpc import StubAdapter, make_state


@pytest.mark.asyncio
async def test_server_update_applies_keep_awake_live(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    reload_calls: list[bool] = []

    def reload_keep_awake() -> None:
        reload_calls.append(state.runtime.storage.load_settings()["keep_awake"])

    state.runtime.reload_keep_awake = reload_keep_awake

    result = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"server": {"keep_awake": True}}},
    )

    assert result["ok"] is True
    assert reload_calls == [True]
    assert result["result"]["general"]["keep_awake"] is True


@pytest.mark.asyncio
async def test_unchanged_keep_awake_value_skips_the_live_seam(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    reload_calls: list[bool] = []
    state.runtime.reload_keep_awake = lambda: reload_calls.append(True)

    await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"server": {}}},
    )

    assert reload_calls == []


@pytest.mark.asyncio
async def test_runtime_without_the_seam_still_saves(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    result = await dispatch_rpc(
        state,
        {"method": "settings.update", "params": {"server": {"keep_awake": False}}},
    )

    assert result["ok"] is True
    assert state.runtime.storage.load_settings()["keep_awake"] is False


@pytest.mark.asyncio
async def test_server_update_applies_timezone_live(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    reload_calls: list[str] = []

    def reload_timezone() -> None:
        reload_calls.append(state.runtime.storage.load_settings()["timezone"])

    state.runtime.reload_timezone = reload_timezone

    result = await dispatch_rpc(
        state,
        {
            "method": "settings.update",
            "params": {"server": {"timezone": "America/New_York"}},
        },
    )

    assert result["ok"] is True
    assert reload_calls == ["America/New_York"]
    assert result["result"]["general"]["timezone"] == "America/New_York"
