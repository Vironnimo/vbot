"""Tests for channel config storage and ChannelService lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.channels import (
    ChannelAdapter,
    ChannelConfig,
    ChannelConfigError,
    ChannelNotFoundError,
    ChannelService,
    ChannelStorage,
)


def make_runtime(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(storage=SimpleNamespace(data_dir=tmp_path))


def make_service(tmp_path: Path) -> ChannelService:
    return ChannelService(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        make_runtime(tmp_path),
    )


def make_config(
    channel_id: str = "tg-assistant",
    *,
    enabled: bool = True,
    allowed_chat_ids: list[int] | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        platform="telegram",
        agent_id="assistant",
        dm_scope="per_conversation",
        allowed_chat_ids=list(allowed_chat_ids or []),
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=enabled,
    )


class BlockingAdapter(ChannelAdapter):
    platform = "telegram"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.sent_messages: list[tuple[str, str]] = []

    async def start(self) -> None:
        self.started.set()
        await asyncio.Future()

    async def stop(self) -> None:
        self.stopped.set()

    async def send(self, message: str, platform_target: str) -> None:
        self.sent_messages.append((message, platform_target))


def test_channel_config_enabled_defaults_true() -> None:
    payload = {
        "id": "tg-assistant",
        "platform": "telegram",
        "agent_id": "assistant",
        "dm_scope": "per_conversation",
        "allowed_chat_ids": [],
        "token_env_var": "TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
    }

    config = ChannelConfig.from_dict(payload)

    assert config.enabled is True


def test_channel_storage_crud_round_trip(tmp_path: Path) -> None:
    # Arrange
    storage = ChannelStorage(tmp_path)
    initial = make_config(allowed_chat_ids=[])
    updated = replace(initial, allowed_chat_ids=[12345], enabled=False)

    # Act
    storage.save(initial)
    loaded = storage.get(initial.id)
    listed = storage.load_all()
    storage.save(updated)
    reloaded = storage.get(initial.id)
    storage.delete(initial.id)

    # Assert
    assert loaded.to_dict() == initial.to_dict()
    assert [item.id for item in listed] == [initial.id]
    assert reloaded.allowed_chat_ids == [12345]
    assert reloaded.enabled is False
    with pytest.raises(ChannelNotFoundError, match=initial.id):
        storage.get(initial.id)


def test_channel_storage_load_all_missing_directory_returns_empty_list(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)

    assert storage.load_all() == []


def test_channel_service_create_rejects_duplicate_ids(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    config = make_config()

    service.create_channel(config)

    with pytest.raises(ChannelConfigError, match="already exists"):
        service.create_channel(config)


@pytest.mark.asyncio
async def test_channel_service_start_and_stop_manage_enabled_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    storage = ChannelStorage(tmp_path)
    enabled = make_config("tg-enabled", enabled=True)
    disabled = make_config("tg-disabled", enabled=False)
    storage.save(enabled)
    storage.save(disabled)

    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    # Act
    service.start()
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    # Assert
    assert service.has_active_channels() is True
    assert "tg-disabled" not in service._adapter_tasks

    # Cleanup
    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_enable_disable_updates_runtime_and_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=False)
    storage.save(config)

    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    hook_calls = 0

    def hook() -> None:
        nonlocal hook_calls
        hook_calls += 1

    service._notify_tool_registration_changed_hook = hook
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    # Act
    service.enable_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    enabled_config = storage.get(config.id)

    service.disable_channel(config.id)
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    disabled_config = storage.get(config.id)
    await asyncio.sleep(0)

    # Assert
    assert enabled_config.enabled is True
    assert disabled_config.enabled is False
    assert hook_calls == 2


@pytest.mark.asyncio
async def test_channel_service_send_routes_to_running_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    # Act
    service.send(config.id, "Hello", "12345")
    await asyncio.sleep(0)

    # Assert
    assert adapter.sent_messages == [("Hello", "12345")]

    # Cleanup
    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


def test_channel_service_send_raises_for_inactive_channel(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ChannelNotFoundError, match="Channel not active"):
        service.send("tg-assistant", "hello", "12345")


def test_channel_service_update_rejects_unknown_fields(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=False)
    storage.save(config)
    service = make_service(tmp_path)

    with pytest.raises(ChannelConfigError, match="Unsupported channel fields"):
        service.update_channel(config.id, unknown_field="value")
