"""Audit-log coverage for Provider credential mutations."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from server.rpc.methods import dispatch_rpc
from tests.server.test_rpc import StubAdapter, make_state


@pytest.mark.asyncio
async def test_provider_key_logs_once_without_secret_or_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    params = {
        "provider_id": "openai",
        "connection_id": "openai:api-key",
        "account": "private_slot",
        "value": "provider-secret-value",
    }

    with caplog.at_level(logging.INFO, logger="vbot.server.rpc.connection_methods"):
        first = await dispatch_rpc(state, {"method": "provider.set_key", "params": params})
        second = await dispatch_rpc(state, {"method": "provider.set_key", "params": params})

    assert first["ok"] is True
    assert second["ok"] is True
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.server.rpc.connection_methods"
        and record.getMessage().startswith("Provider credential saved")
    ]
    assert messages == [
        "Provider credential saved (provider=openai connection=api-key configured=true usable=True)"
    ]
    assert "provider-secret-value" not in " ".join(messages)
    assert "private_slot" not in " ".join(messages)


@pytest.mark.asyncio
async def test_provider_key_removal_logs_only_when_credential_existed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = make_state(tmp_path, StubAdapter())
    target = {
        "provider_id": "openai",
        "connection_id": "openai:api-key",
        "account": "private_slot",
    }
    await dispatch_rpc(
        state,
        {
            "method": "provider.set_key",
            "params": {**target, "value": "provider-secret-value"},
        },
    )
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="vbot.server.rpc.connection_methods"):
        first = await dispatch_rpc(state, {"method": "provider.unset_key", "params": target})
        second = await dispatch_rpc(state, {"method": "provider.unset_key", "params": target})

    assert first["result"]["removed"] is True
    assert second["result"]["removed"] is False
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "vbot.server.rpc.connection_methods"
        and record.getMessage().startswith("Provider credential removed")
    ]
    assert messages == [
        "Provider credential removed "
        "(provider=openai connection=api-key configured=False usable=False)"
    ]
    assert "provider-secret-value" not in " ".join(messages)
    assert "private_slot" not in " ".join(messages)
