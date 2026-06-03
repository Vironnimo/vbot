"""Tests for channel config storage and ChannelService lifecycle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.attachments import AttachmentStore
from core.channels import (
    ChannelAdapter,
    ChannelConfig,
    ChannelConfigError,
    ChannelNotFoundError,
    ChannelService,
    ChannelStorage,
)
from core.channels.adapter import FileData
from core.channels.telegram import TelegramChannelAdapter
from core.chat.commands import NotACommand


class AgentStoreStub:
    def __init__(self, *, known_agent_ids: set[str] | None = None) -> None:
        self._known_agent_ids = set(known_agent_ids or {"assistant"})

    def get(self, agent_id: str) -> SimpleNamespace:
        if agent_id not in self._known_agent_ids:
            raise KeyError(agent_id)
        return SimpleNamespace(id=agent_id)


def make_runtime(
    tmp_path: Path,
    *,
    known_agent_ids: set[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        storage=SimpleNamespace(data_dir=tmp_path),
        agents=AgentStoreStub(known_agent_ids=known_agent_ids),
    )


def make_service(
    tmp_path: Path,
    *,
    known_agent_ids: set[str] | None = None,
    attachment_store: AttachmentStore | None = None,
) -> ChannelService:
    return ChannelService(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        make_runtime(tmp_path, known_agent_ids=known_agent_ids),
        attachment_store=attachment_store,
        command_dispatcher=cast(Any, SimpleNamespace(dispatch=lambda *_args: NotACommand())),
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
        self.sent_messages: list[tuple[str | None, str]] = []

    async def start(self) -> None:
        self.started.set()
        await asyncio.Future()

    async def stop(self) -> None:
        self.stopped.set()

    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
    ) -> None:
        self.sent_messages.append((message, platform_target))


class FailingAdapter(ChannelAdapter):
    platform = "telegram"

    def __init__(self, *, fail_on_start: bool) -> None:
        self._fail_on_start = fail_on_start
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        if self._fail_on_start:
            raise RuntimeError("adapter failed")
        await asyncio.Future()

    async def stop(self) -> None:
        self.stopped.set()

    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
    ) -> None:
        return


class DelayedStopAdapter(ChannelAdapter):
    platform = "telegram"

    def __init__(
        self,
        *,
        label: str,
        stop_gate: asyncio.Event,
        events: list[str],
    ) -> None:
        self.label = label
        self._stop_gate = stop_gate
        self._events = events
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self._events.append(f"start:{self.label}")
        self.started.set()
        await asyncio.Future()

    async def stop(self) -> None:
        self._events.append(f"stop:{self.label}:begin")
        await self._stop_gate.wait()
        self._events.append(f"stop:{self.label}:end")
        self.stopped.set()

    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
    ) -> None:
        return


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("Timed out waiting for condition")
        await asyncio.sleep(0)


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


def test_channel_storage_validates_channel_json_on_read(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config_dir = tmp_path / "channels" / "tg-assistant"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("channel.json").write_text(
        json.dumps({**make_config().to_dict(), "enabled": "true"}), encoding="utf-8"
    )

    with pytest.raises(ChannelConfigError, match=r"\$\.enabled: must be a boolean"):
        storage.get("tg-assistant")


def test_channel_service_create_rejects_duplicate_ids(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    config = make_config()

    service.create_channel(config)

    with pytest.raises(ChannelConfigError, match="already exists"):
        service.create_channel(config)


def test_channel_service_adapter_factory_builds_telegram_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "token")
    service = make_service(tmp_path)

    adapter = service._create_adapter(make_config())

    assert isinstance(adapter, TelegramChannelAdapter)


def test_channel_service_adapter_factory_injects_attachment_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "token")
    attachment_store = cast(AttachmentStore, object())
    service = make_service(tmp_path, attachment_store=attachment_store)

    adapter = service._create_adapter(make_config())

    assert isinstance(adapter, TelegramChannelAdapter)
    assert adapter._attachment_store is attachment_store


def test_channel_service_create_validates_agent_exists(tmp_path: Path) -> None:
    service = make_service(tmp_path, known_agent_ids={"main"})

    with pytest.raises(ChannelConfigError, match="Unknown agent_id"):
        service.create_channel(make_config())


def test_channel_service_update_validates_agent_exists(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=False)
    storage.save(config)
    service = make_service(tmp_path, known_agent_ids={"assistant"})

    with pytest.raises(ChannelConfigError, match="Unknown agent_id"):
        service.update_channel(config.id, agent_id="missing-agent")


def test_channel_service_start_marks_missing_agent_channel_failed(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)
    service = make_service(tmp_path, known_agent_ids={"main"})

    service.start()

    assert service.has_active_channels() is False
    assert service.is_failed(config.id) is True
    assert service.failure_reason(config.id) == "Unknown agent_id: assistant"


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
async def test_channel_service_aclose_awaits_adapter_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config("tg-enabled", enabled=True)
    storage.save(config)
    stop_gate = asyncio.Event()
    lifecycle_events: list[str] = []
    adapter = DelayedStopAdapter(label="first", stop_gate=stop_gate, events=lifecycle_events)
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    service.start()
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    close_task = asyncio.create_task(service.aclose())
    await wait_until(lambda: "stop:first:begin" in lifecycle_events)

    assert not close_task.done()

    stop_gate.set()
    await asyncio.wait_for(close_task, timeout=1)

    assert adapter.stopped.is_set()
    assert service._adapter_tasks == {}
    assert service._adapter_stop_tasks == {}


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
    await service.send(config.id, "Hello", "12345")

    # Assert
    assert adapter.sent_messages == [("Hello", "12345")]

    # Cleanup
    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_notifies_hook_when_adapter_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    hook_calls = 0

    def hook() -> None:
        nonlocal hook_calls
        hook_calls += 1

    service._notify_tool_registration_changed_hook = hook
    monkeypatch.setattr(
        service, "_create_adapter", lambda _config: FailingAdapter(fail_on_start=True)
    )
    monkeypatch.setattr(service, "_schedule_restart", lambda _channel_id: None)

    service.start()
    await wait_until(lambda: config.id not in service._adapter_tasks)

    assert service.has_active_channels() is False
    assert hook_calls >= 2


@pytest.mark.asyncio
async def test_channel_service_ignores_stale_adapter_task_done_callback(
    tmp_path: Path,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    stale_task = asyncio.create_task(asyncio.sleep(0))
    current_task = asyncio.create_task(asyncio.sleep(60))
    await stale_task

    service._adapters[config.id] = adapter
    service._adapter_tasks[config.id] = current_task

    service._on_adapter_task_done(config.id, stale_task)

    assert service._adapters[config.id] is adapter
    assert service._adapter_tasks[config.id] is current_task

    current_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await current_task


@pytest.mark.asyncio
async def test_channel_service_send_raises_for_inactive_channel(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ChannelNotFoundError, match="Channel not active"):
        await service.send("tg-assistant", "hello", "12345")


def test_channel_service_update_rejects_unknown_fields(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=False)
    storage.save(config)
    service = make_service(tmp_path)

    with pytest.raises(ChannelConfigError, match="Unsupported channel fields"):
        service.update_channel(config.id, unknown_field="value")


def test_channel_service_create_rolls_back_when_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    service = make_service(tmp_path)
    config = make_config(enabled=True)

    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)

    def fail_start_channel(
        _channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        raise ChannelConfigError("start failed")

    monkeypatch.setattr(service, "start_channel", fail_start_channel)

    with pytest.raises(ChannelConfigError, match="start failed"):
        service.create_channel(config)

    with pytest.raises(ChannelNotFoundError):
        storage.get(config.id)


def test_channel_service_update_rolls_back_when_restart_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    original = make_config(enabled=False)
    storage.save(original)
    service = make_service(tmp_path)

    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)

    def fail_start_channel(
        _channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        raise ChannelConfigError("restart failed")

    monkeypatch.setattr(service, "start_channel", fail_start_channel)

    with pytest.raises(ChannelConfigError, match="restart failed"):
        service.update_channel(original.id, enabled=True)

    assert storage.get(original.id).to_dict() == original.to_dict()


@pytest.mark.asyncio
async def test_channel_service_update_waits_for_adapter_stop_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    stop_gate = asyncio.Event()
    lifecycle_events: list[str] = []
    created: list[DelayedStopAdapter] = []

    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        label = "old" if not created else "new"
        adapter = DelayedStopAdapter(label=label, stop_gate=stop_gate, events=lifecycle_events)
        created.append(adapter)
        return adapter

    monkeypatch.setattr(service, "_create_adapter", create_adapter)

    service.start()
    await asyncio.wait_for(created[0].started.wait(), timeout=1)

    service.update_channel(config.id, token_env_var="TELEGRAM_BOT_TOKEN_OTHER")
    await wait_until(lambda: "stop:old:begin" in lifecycle_events)

    assert "start:new" not in lifecycle_events

    stop_gate.set()
    await wait_until(lambda: "start:new" in lifecycle_events)
    assert lifecycle_events.index("stop:old:end") < lifecycle_events.index("start:new")

    service.stop()


@pytest.mark.asyncio
async def test_channel_service_restarts_failed_adapter_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    created: list[FailingAdapter] = []
    starts = 0

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        nonlocal starts
        starts += 1
        adapter = FailingAdapter(fail_on_start=starts == 1)
        created.append(adapter)
        return adapter

    delays: list[float] = []
    original_restart_delay = service._restart_delay_seconds

    def immediate_restart_delay(attempt: int) -> float:
        delays.append(original_restart_delay(attempt))
        return 0.0

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_restart_delay_seconds", immediate_restart_delay)

    service.start()
    await wait_until(lambda: len(created) >= 2)
    await asyncio.wait_for(created[1].started.wait(), timeout=1)

    assert delays == [1.0]
    assert service.has_active_channels() is True
    assert config.id not in service._failed_channels

    service.stop()
    await asyncio.wait_for(created[-1].stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_marks_channel_failed_after_max_restart_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    created: list[FailingAdapter] = []

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        adapter = FailingAdapter(fail_on_start=True)
        created.append(adapter)
        return adapter

    delays: list[float] = []
    original_restart_delay = service._restart_delay_seconds

    def immediate_restart_delay(attempt: int) -> float:
        delays.append(original_restart_delay(attempt))
        return 0.0

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_restart_delay_seconds", immediate_restart_delay)

    service.start()
    await wait_until(
        lambda: (
            config.id in service._failed_channels
            and config.id not in service._adapter_tasks
            and config.id not in service._adapter_restart_tasks
        )
    )

    assert delays == [1.0, 2.0, 4.0]
    assert len(created) == 4
    assert service.has_active_channels() is False
    assert service._adapter_restart_attempts[config.id] == 3

    service.stop()
