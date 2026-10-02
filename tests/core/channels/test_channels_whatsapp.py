"""Channels: WhatsApp support setup and pairing as owned Channel operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.channels import ChannelService
from core.channels.config import ChannelConfig, ChannelConfigError, ChannelError

from .channels_test_support import make_service

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


async def whatsapp_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ChannelService, Mock]:
    """Return a service with one disabled WhatsApp Channel and its adapter-start double."""
    service = make_service(tmp_path)
    await service.create_channel(
        ChannelConfig(id="wa", platform="whatsapp", agent_id="assistant", enabled=False)
    )
    start_adapter = Mock()
    monkeypatch.setattr(service, "start_channel", start_adapter)
    return service, start_adapter


@pytest.mark.asyncio
async def test_whatsapp_setup_is_idempotent_and_shutdown_cancels_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _start_adapter = await whatsapp_service(tmp_path, monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def install(_: Path) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    install_bridge = AsyncMock(side_effect=install)
    monkeypatch.setattr("core.channels._whatsapp_setup.install_bridge", install_bridge)
    monkeypatch.setattr("core.channels._whatsapp_setup.bridge_ready", lambda _: False)
    try:
        await service.setup_whatsapp("wa")
        await started.wait()
        assert (await service.setup_whatsapp("wa"))["setup"] == "installing"
        install_bridge.assert_awaited_once()
        assert service.setup_activities() == [{"channel_id": "wa", "state": "running"}]
    finally:
        await service.aclose()
    assert cancelled.is_set()
    assert service.setup_activities() == []


@pytest.mark.asyncio
async def test_failed_whatsapp_setup_is_background_activity_until_the_channel_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _start_adapter = await whatsapp_service(tmp_path, monkeypatch)
    install_bridge = AsyncMock(side_effect=ChannelError("Node.js is missing"))
    monkeypatch.setattr("core.channels._whatsapp_setup.install_bridge", install_bridge)
    try:
        await service.setup_whatsapp("wa")
        async with asyncio.timeout(2):
            while (await service.whatsapp_status("wa"))["setup"] != "failed":
                await asyncio.sleep(0)
        assert service.setup_activities() == [
            {
                "channel_id": "wa",
                "state": "failed",
                "error": "setup_failed",
                "message": "Node.js is missing",
            }
        ]
        await service.delete_channel("wa")
        assert service.setup_activities() == []
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["setup", "pair"])
async def test_whatsapp_operation_blocks_channel_changes_until_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    service, start_adapter = await whatsapp_service(tmp_path, monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold(_: object) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"installed": True, "state": "disconnected"}

    if operation == "setup":
        # Setup returns at once and leaves the installation running.
        monkeypatch.setattr("core.channels._whatsapp_setup.install_bridge", hold)
        running = asyncio.create_task(service.setup_whatsapp("wa"))
        await running
    else:
        # Pairing holds the Channel's operation while it prepares the connection.
        monkeypatch.setattr(service, "whatsapp_status", hold)
        running = asyncio.create_task(service.pair_whatsapp("wa"))
    changes = {
        "enable": lambda: service.enable_channel("wa"),
        "disable": lambda: service.disable_channel("wa"),
        "restart": lambda: service.restart_channel("wa"),
        "update": lambda: service.update_channel("wa", response_mode="all"),
        "delete": lambda: service.delete_channel("wa"),
    }
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        for change in changes.values():
            with pytest.raises(ChannelError, match="^Wait for"):
                await change()
        assert not service.list_channels()[0].enabled
        start_adapter.assert_not_called()

        release.set()
        await running
        if operation == "pair":
            # Pairing ends by enabling the Channel and starting its adapter.
            assert service.list_channels()[0].enabled
            start_adapter.assert_called_once()
        else:
            async with asyncio.timeout(2):
                while (await service.whatsapp_status("wa"))["setup"] != "ready":
                    await asyncio.sleep(0)
        await service.delete_channel("wa")
        assert service.list_channels() == []
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await service.aclose()


@pytest.mark.asyncio
async def test_whatsapp_setup_refuses_an_enabled_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An enabled Channel whose adapter is not running (e.g. awaiting recovery)
    # would be restarted over a half-installed bridge.
    service, _start_adapter = await whatsapp_service(tmp_path, monkeypatch)
    await service.enable_channel("wa")
    install = AsyncMock()
    monkeypatch.setattr("core.channels._whatsapp_setup.install_bridge", install)
    try:
        with pytest.raises(ChannelConfigError):
            await service.setup_whatsapp("wa")
        assert "setup" not in await service.whatsapp_status("wa")
        install.assert_not_awaited()
    finally:
        await service.aclose()
