"""Channels: service behavior."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from core.attachments import AttachmentStore
from core.channels import (
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
    ChannelStorage,
)
from core.channels.adapter import (
    DeniedChatLog,
    RouteFacts,
)
from core.channels.discord import DiscordChannelAdapter
from core.channels.telegram import TelegramChannelAdapter
from core.database import DatabaseUnavailableError
from tests.core.channels.channels_helpers import (
    BlockingAdapter,
    DelayedStopAdapter,
    make_config,
    make_service,
    wait_until,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class DeniedAwareAdapter(BlockingAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._denied_chat_log = DeniedChatLog()

    def denied_chats(self) -> list[Any]:
        return self._denied_chat_log.entries()


@pytest.mark.asyncio
async def test_channel_service_create_rejects_duplicate_ids(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    config = make_config(enabled=False)

    await service.create_channel(config)

    with pytest.raises(ChannelConfigError):
        await service.create_channel(config)


def test_channel_service_adapter_factory_builds_telegram_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "token")
    service = make_service(tmp_path)

    adapter = service._create_adapter(make_config())

    assert isinstance(adapter, TelegramChannelAdapter)


def test_channel_service_adapter_factory_builds_discord_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN_DC_ASSISTANT", "token")
    service = make_service(tmp_path)

    adapter = service._create_adapter(
        make_config(
            "dc-assistant",
            platform="discord",
        )
    )

    assert isinstance(adapter, DiscordChannelAdapter)


def test_channel_service_adapter_factory_injects_attachment_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "token")
    attachment_store = cast(AttachmentStore, object())
    service = make_service(tmp_path, attachment_store=attachment_store)

    adapter = service._create_adapter(make_config())

    assert isinstance(adapter, TelegramChannelAdapter)
    assert adapter._transport._attachment_store is attachment_store


@pytest.mark.asyncio
async def test_channel_service_create_validates_agent_exists(tmp_path: Path) -> None:
    service = make_service(tmp_path, known_agent_ids={"main"})

    with pytest.raises(ChannelConfigError):
        await service.create_channel(make_config())


def test_channel_service_update_validates_agent_exists(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=False)
    storage.save(config)
    service = make_service(tmp_path, known_agent_ids={"assistant"})

    with pytest.raises(ChannelConfigError):
        service.update_channel(config.id, agent_id="missing-agent")


def test_record_chat_id_migration_swaps_allowlist_and_persists(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    storage.save(make_config(allowed_chat_ids=[-500, 777]))
    service = make_service(tmp_path)

    service.record_chat_id_migration("tg-assistant", "-500", "-100500")

    assert storage.get("tg-assistant").allowed_chat_ids == ["-100500", "777"]

    # Idempotent: once the old id is gone, a repeat call changes nothing.
    service.record_chat_id_migration("tg-assistant", "-500", "-100999")
    assert storage.get("tg-assistant").allowed_chat_ids == ["-100500", "777"]


def test_channel_service_start_tolerates_corrupt_config(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    storage.save(make_config("tg-valid", enabled=True))
    broken_dir = tmp_path / "channels" / "tg-broken"
    broken_dir.mkdir(parents=True)
    broken_dir.joinpath("channel.json").write_text(
        json.dumps(
            {"format_version": 1, **make_config("tg-broken").to_dict(), "enabled": "not-a-bool"}
        ),
        encoding="utf-8",
    )
    service = make_service(tmp_path)

    # A corrupt channel.json must never abort startup. No running loop here, so the valid
    # channel only logs instead of launching, but start() must complete without raising.
    service.start()

    assert [config.id for config in service.list_channels()] == ["tg-valid"]


def test_channel_service_start_marks_missing_agent_channel_failed(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)
    service = make_service(tmp_path, known_agent_ids={"main"})

    service.start()

    assert service.has_active_channels() is False
    assert service.has_enabled_channels() is False
    assert service.is_failed(config.id) is True
    failure_reason = service.failure_reason(config.id)
    assert failure_reason
    assert "assistant" in failure_reason


@pytest.mark.asyncio
async def test_channel_config_create_delete_controls_tool_registration_without_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(tmp_path)
    hook_calls = 0

    def hook() -> None:
        nonlocal hook_calls
        hook_calls += 1

    service._notify_tool_registration_changed_hook = hook
    monkeypatch.setattr(service, "_create_adapter", lambda _config: BlockingAdapter())
    # No adapter runs: registration follows the persisted config alone.
    monkeypatch.setattr(service, "start_channel", lambda *_args, **_kwargs: None)

    await service.create_channel(make_config(enabled=True))

    assert service.has_enabled_channels() is True
    assert service.has_active_channels() is False
    assert hook_calls == 1

    await service.delete_channel("tg-assistant")

    assert service.has_enabled_channels() is False
    assert hook_calls == 2


@pytest.mark.asyncio
async def test_channel_state_follows_channel_create_and_delete(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        await service.create_channel(make_config(enabled=False))
        await service._state.snapshot_participant_role("tg-assistant", "-100", "50", "Alice")
        service._state.save_update_offset("tg-assistant", 42)

        await service.delete_channel("tg-assistant")

        # A late write of a stopping adapter cannot resurrect deleted state.
        with pytest.raises(ChannelNotFoundError):
            service._state.save_update_offset("tg-assistant", 43)
        with service.database.read() as connection:
            for table in ("channel_participants", "channel_polling"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

        await service.create_channel(make_config(enabled=False))
        assert service._state.load_update_offset("tg-assistant") == 0
        assert (await service.channel_access("tg-assistant"))["groups"] == []
    finally:
        service.close()


def _registered_channels(service: Any) -> list[str]:
    with service.database.read() as connection:
        return [row[0] for row in connection.execute("SELECT channel_id FROM channels")]


@pytest.mark.asyncio
async def test_channel_create_and_delete_write_channels_db_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    storage = ChannelStorage(tmp_path)
    loop_thread = threading.get_ident()
    release = threading.Event()
    calls: list[tuple[str, bool]] = []
    real_reset = service._state.reset
    real_unregister = service._state.unregister

    def held(name: str, write: Any) -> Any:
        def call(channel_id: str) -> None:
            calls.append((name, threading.get_ident() != loop_thread))
            assert release.wait(timeout=5)
            write(channel_id)

        return call

    monkeypatch.setattr(service._state, "reset", held("reset", real_reset))
    monkeypatch.setattr(service._state, "unregister", held("unregister", real_unregister))
    try:
        creating = asyncio.create_task(service.create_channel(make_config(enabled=False)))
        await wait_until(lambda: calls == [("reset", True)])
        # channel.json is written before the registration; the Event Loop keeps
        # serving meanwhile and refuses other changes of this Channel.
        assert storage.get("tg-assistant").id == "tg-assistant"
        with pytest.raises(ChannelError, match="finish being created or removed"):
            service.update_channel("tg-assistant", enabled=True)
        with pytest.raises(ChannelError, match="finish being created or removed"):
            await service.delete_channel("tg-assistant")
        release.set()
        await asyncio.wait_for(creating, timeout=5)
        assert _registered_channels(service) == ["tg-assistant"]

        release.clear()
        deleting = asyncio.create_task(service.delete_channel("tg-assistant"))
        await wait_until(lambda: calls[-1] == ("unregister", True))
        # channel.json goes first; the registration follows.
        with pytest.raises(ChannelNotFoundError):
            storage.get("tg-assistant")
        with pytest.raises(ChannelError, match="finish being created or removed"):
            await service.create_channel(make_config(enabled=False))
        release.set()
        await asyncio.wait_for(deleting, timeout=5)
        assert _registered_channels(service) == []
        assert service._pending_config_changes == set()
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
async def test_a_failed_registration_removes_the_created_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    storage = ChannelStorage(tmp_path)

    def fail(_channel_id: str) -> None:
        raise DatabaseUnavailableError("channels is busy")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(service._state, "reset", fail)
            with pytest.raises(DatabaseUnavailableError, match="channels is busy"):
                await service.create_channel(make_config(enabled=False))

        with pytest.raises(ChannelNotFoundError):
            storage.get("tg-assistant")
        assert _registered_channels(service) == []
        await service.create_channel(make_config(enabled=False))
        assert _registered_channels(service) == ["tg-assistant"]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_a_create_cancelled_during_registration_removes_config_and_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    storage = ChannelStorage(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    real_reset = service._state.reset

    def reset(channel_id: str) -> None:
        entered.set()
        assert release.wait(timeout=5)
        real_reset(channel_id)

    monkeypatch.setattr(service._state, "reset", reset)
    try:
        creating = asyncio.create_task(service.create_channel(make_config(enabled=False)))
        await wait_until(entered.is_set)
        creating.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(creating, timeout=5)

        with pytest.raises(ChannelNotFoundError):
            storage.get("tg-assistant")
        assert _registered_channels(service) == []
        assert service._pending_config_changes == set()
    finally:
        release.set()
        service.close()


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
async def test_ensure_outbound_session_delegates_to_active_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    storage.save(make_config("tg-enabled", enabled=True))
    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    service.start()
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    route = service.ensure_outbound_session("tg-enabled", "12345")
    assert route == RouteFacts(agent_id="assistant", session_id="ch-blocking-12345")

    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


def test_ensure_outbound_session_raises_for_inactive_channel(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ChannelNotFoundError):
        service.ensure_outbound_session("tg-enabled", "12345")


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
async def test_channel_service_send_raises_for_inactive_channel(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ChannelNotFoundError):
        await service.send("tg-assistant", "hello", "12345")


@pytest.mark.asyncio
async def test_channel_service_denied_chats_delegates_to_running_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    adapter = DeniedAwareAdapter()
    adapter._denied_chat_log.record(chat_id="777", kind="direct", display_name="Julian")
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    entries = service.denied_chats(config.id)
    assert [entry.chat_id for entry in entries] == ["777"]

    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)

    assert service.denied_chats(config.id) == []


def test_channel_service_denied_chats_empty_for_unknown_channel(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    assert service.denied_chats("tg-assistant") == []


@pytest.mark.asyncio
async def test_await_adapter_shutdown_logs_real_exception_at_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # By the time the stop task awaits the adapter task it was already popped from
    # _adapter_tasks, so its own done-callback returns early without logging. A real
    # shutdown exception would surface nowhere unless _await_adapter_shutdown logs it.
    service = make_service(tmp_path)

    async def raise_shutdown_error() -> None:
        raise RuntimeError("adapter shutdown blew up")

    failing_task = asyncio.create_task(raise_shutdown_error())
    await wait_until(failing_task.done)

    with caplog.at_level(logging.ERROR, logger="vbot.channels"):
        await service._await_adapter_shutdown("tg-assistant", failing_task)

    error_records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any(
        "shutdown raised during stop" in record.getMessage()
        and "tg-assistant" in record.getMessage()
        for record in error_records
    )
    # The traceback must be attached so the underlying error is diagnosable.
    assert any(record.exc_info is not None for record in error_records)


@pytest.mark.asyncio
async def test_await_adapter_shutdown_keeps_cancelled_silent(tmp_path: Path) -> None:
    # Cooperative cancel cleanup is the normal path and must not be logged as an error.
    service = make_service(tmp_path)

    async def block_forever() -> None:
        await asyncio.Future()

    cancelled_task = asyncio.create_task(block_forever())
    await asyncio.sleep(0)
    cancelled_task.cancel()

    await service._await_adapter_shutdown("tg-assistant", cancelled_task)

    assert cancelled_task.cancelled()
