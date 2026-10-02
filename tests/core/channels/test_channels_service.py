"""Channels: ChannelService configuration changes and adapter lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast, override

import pytest

from core.attachments import AttachmentStore
from core.channels import (
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelNotFoundError,
    ChannelStorage,
)
from core.channels.adapter import DeniedChatLog, RouteFacts
from core.channels.discord import DiscordChannelAdapter
from core.channels.telegram import TelegramChannelAdapter
from core.database import DatabaseUnavailableError
from tests.core.channels.channels_test_support import (
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

    @override
    def denied_chats(self) -> list[Any]:
        return self._denied_chat_log.entries()


class ShutdownFailureAdapter(BlockingAdapter):
    """An adapter whose task ends with an error instead of its cancellation."""

    @override
    async def start(self) -> None:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise RuntimeError("adapter shutdown blew up") from None


def _registered_channels(service: Any) -> list[str]:
    with service.database.read() as connection:
        return [row[0] for row in connection.execute("SELECT channel_id FROM channels")]


@pytest.mark.asyncio
async def test_create_and_update_refuse_invalid_changes(tmp_path: Path) -> None:
    service = make_service(tmp_path, known_agent_ids={"assistant"})
    try:
        await service.create_channel(make_config(enabled=False))

        refused = [
            service.create_channel(make_config(enabled=False)),
            service.create_channel(
                replace(make_config("tg-other", enabled=False), agent_id="missing-agent")
            ),
            service.update_channel("tg-assistant", agent_id="missing-agent"),
            service.update_channel("tg-assistant", unknown_field="value"),
        ]
        for change in refused:
            with pytest.raises(ChannelConfigError):
                await change
    finally:
        service.close()

    storage = ChannelStorage(tmp_path)
    assert [config.id for config in storage.load_all()] == ["tg-assistant"]
    assert storage.get("tg-assistant").agent_id == "assistant"


@pytest.mark.parametrize(
    ("config", "token_env_var", "adapter_type"),
    [
        (make_config(), "TELEGRAM_BOT_TOKEN_TG_ASSISTANT", TelegramChannelAdapter),
        (
            make_config("dc-assistant", platform="discord"),
            "DISCORD_BOT_TOKEN_DC_ASSISTANT",
            DiscordChannelAdapter,
        ),
    ],
    ids=["telegram", "discord"],
)
def test_channel_service_builds_the_adapter_of_the_configured_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: ChannelConfig,
    token_env_var: str,
    adapter_type: type,
) -> None:
    monkeypatch.setenv(token_env_var, "token")
    attachment_store = cast(AttachmentStore, object())
    service = make_service(tmp_path, attachment_store=attachment_store)
    try:
        adapter = service._create_adapter(config)
    finally:
        service.close()

    assert isinstance(adapter, adapter_type)
    if isinstance(adapter, TelegramChannelAdapter):
        assert adapter._transport._attachment_store is attachment_store


def test_channel_service_start_degrades_per_channel(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    storage.save(replace(make_config("tg-valid", enabled=True), agent_id="main"))
    storage.save(make_config("tg-orphan", enabled=True))
    broken_dir = tmp_path / "channels" / "tg-broken"
    broken_dir.mkdir(parents=True)
    broken_dir.joinpath("channel.json").write_text(
        json.dumps(
            {"format_version": 1, **make_config("tg-broken").to_dict(), "enabled": "not-a-bool"}
        ),
        encoding="utf-8",
    )
    service = make_service(tmp_path, known_agent_ids={"main"})

    # A corrupt channel.json is skipped and a Channel of an unknown Agent is
    # marked failed. Without a running loop the valid Channel only logs instead
    # of launching; startup itself completes.
    service.start()

    try:
        assert [config.id for config in service.list_channels()] == ["tg-orphan", "tg-valid"]
        assert service.has_active_channels() is False
        assert service.has_enabled_channels() is True
        assert service.is_failed("tg-orphan") is True
        assert "assistant" in (service.failure_reason("tg-orphan") or "")
        assert service.is_failed("tg-valid") is False
    finally:
        service.stop()
        service.close()


@pytest.mark.asyncio
async def test_tool_registration_follows_enabled_configs_not_adapter_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ChannelStorage(tmp_path)
    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    hook_calls = 0

    def hook() -> None:
        nonlocal hook_calls
        hook_calls += 1

    service._notify_tool_registration_changed_hook = hook
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    try:
        await service.create_channel(make_config(enabled=False))
        assert hook_calls == 0

        await service.enable_channel("tg-assistant")
        await asyncio.wait_for(adapter.started.wait(), timeout=1)
        assert (storage.get("tg-assistant").enabled, hook_calls) == (True, 1)

        await service.disable_channel("tg-assistant")
        await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
        assert (storage.get("tg-assistant").enabled, hook_calls) == (False, 2)
        await service.delete_channel("tg-assistant")
        assert hook_calls == 2

        # With no adapter running, registration follows the persisted config alone.
        monkeypatch.setattr(service, "start_channel", lambda *_args, **_kwargs: None)
        await service.create_channel(make_config(enabled=True))
        assert service.has_enabled_channels() is True
        assert service.has_active_channels() is False
        assert hook_calls == 3

        await service.delete_channel("tg-assistant")
        assert service.has_enabled_channels() is False
        assert hook_calls == 4
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_service_serves_running_channels_until_they_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ChannelStorage(tmp_path)
    storage.save(make_config("tg-enabled", enabled=True))
    storage.save(make_config("tg-disabled", enabled=False))
    service = make_service(tmp_path)
    adapter = DeniedAwareAdapter()
    adapter._denied_chat_log.record(chat_id="777", kind="direct", display_name="Julian")
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    try:
        service.start()
        await asyncio.wait_for(adapter.started.wait(), timeout=1)

        assert service.has_active_channels() is True
        assert service.is_running("tg-enabled")
        assert not service.is_running("tg-disabled")
        route = await service.ensure_outbound_session("tg-enabled", "12345")
        assert route == RouteFacts(agent_id="assistant", session_id="ch-blocking-12345")
        assert [entry.chat_id for entry in service.denied_chats("tg-enabled")] == ["777"]
        assert service.denied_chats("tg-disabled") == []

        service.stop()
        await asyncio.wait_for(adapter.stopped.wait(), timeout=1)

        assert service.denied_chats("tg-enabled") == []
        with pytest.raises(ChannelNotFoundError):
            await service.ensure_outbound_session("tg-enabled", "12345")
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_channel_state_follows_channel_create_and_delete(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        await service.create_channel(make_config(enabled=False))
        await service._state.snapshot_participant_role("tg-assistant", "-100", "50", "Alice")
        service._state.save_update_offset("tg-assistant", 7001, 42)

        await service.delete_channel("tg-assistant")

        # A late write of a stopping adapter cannot resurrect deleted state.
        with pytest.raises(ChannelNotFoundError):
            service._state.save_update_offset("tg-assistant", 7001, 43)
        with service.database.read() as connection:
            for table in ("channel_participants", "channel_polling"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

        await service.create_channel(make_config(enabled=False))
        assert service._state.load_update_offset("tg-assistant", 7001) == 0
        assert (await service.channel_access("tg-assistant"))["groups"] == []
    finally:
        service.close()


@pytest.mark.asyncio
async def test_channel_create_and_delete_write_channels_db_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service(tmp_path)
    storage = ChannelStorage(tmp_path)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    # The worker thread waits until the test releases it; ``finally`` always does.
    release = threading.Event()
    entered = {"reset": asyncio.Event(), "unregister": asyncio.Event()}
    calls: list[tuple[str, bool]] = []
    real_reset = service._state.reset
    real_unregister = service._state.unregister

    def held(name: str, write: Any) -> Any:
        def call(channel_id: str, *args: Any) -> None:
            calls.append((name, threading.get_ident() != loop_thread))
            loop.call_soon_threadsafe(entered[name].set)
            release.wait()
            write(channel_id, *args)

        return call

    monkeypatch.setattr(service._state, "reset", held("reset", real_reset))
    monkeypatch.setattr(service._state, "unregister", held("unregister", real_unregister))
    try:
        creating = asyncio.create_task(service.create_channel(make_config(enabled=False)))
        await entered["reset"].wait()
        assert calls == [("reset", True)]
        # channel.json is written before the registration; the Event Loop keeps
        # serving meanwhile and refuses other changes of this Channel.
        assert storage.get("tg-assistant").id == "tg-assistant"
        with pytest.raises(ChannelError, match="current change of this Channel"):
            await service.update_channel("tg-assistant", enabled=True)
        with pytest.raises(ChannelError, match="current change of this Channel"):
            await service.delete_channel("tg-assistant")
        release.set()
        await creating
        assert _registered_channels(service) == ["tg-assistant"]

        release.clear()
        deleting = asyncio.create_task(service.delete_channel("tg-assistant"))
        await entered["unregister"].wait()
        assert calls[-1] == ("unregister", True)
        # channel.json goes first; the registration follows.
        with pytest.raises(ChannelNotFoundError):
            storage.get("tg-assistant")
        with pytest.raises(ChannelError, match="current change of this Channel"):
            await service.create_channel(make_config(enabled=False))
        release.set()
        await deleting
        assert _registered_channels(service) == []
        assert service._pending_config_changes == set()
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_a_create_interrupted_during_registration_leaves_no_config_or_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    service = make_service(tmp_path)
    storage = ChannelStorage(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    real_reset = service._state.reset

    def reset(channel_id: str, platform: str) -> None:
        entered.set()
        if outcome == "failed":
            raise DatabaseUnavailableError("channels is busy")
        assert release.wait(timeout=5)
        real_reset(channel_id, platform)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(service._state, "reset", reset)
            creating = asyncio.create_task(service.create_channel(make_config(enabled=False)))
            await wait_until(entered.is_set)
            if outcome == "cancelled":
                creating.cancel()
            release.set()
            expected = DatabaseUnavailableError if outcome == "failed" else asyncio.CancelledError
            with pytest.raises(expected):
                await asyncio.wait_for(creating, timeout=5)

        with pytest.raises(ChannelNotFoundError):
            storage.get("tg-assistant")
        assert _registered_channels(service) == []
        assert service._pending_config_changes == set()
        await service.create_channel(make_config(enabled=False))
        assert _registered_channels(service) == ["tg-assistant"]
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
async def test_config_changes_persist_off_the_event_loop_before_touching_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ChannelStorage(tmp_path)
    storage.save(make_config(enabled=False))
    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    loop_thread = threading.get_ident()
    release = threading.Event()
    saves: list[tuple[bool, bool]] = []
    real_save = service._storage.save

    def held_save(config: Any) -> None:
        saves.append((config.enabled, threading.get_ident() != loop_thread))
        assert release.wait(timeout=5)
        real_save(config)

    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    monkeypatch.setattr(service._storage, "save", held_save)
    try:
        enabling = asyncio.create_task(service.enable_channel("tg-assistant"))
        await wait_until(lambda: saves == [(True, True)])
        # The Event Loop keeps serving; the adapter waits for the write and
        # every other change of this Channel is refused meanwhile.
        assert not service.is_running("tg-assistant")
        with pytest.raises(ChannelError, match="current change of this Channel"):
            await service.disable_channel("tg-assistant")
        with pytest.raises(ChannelError, match="current change of this Channel"):
            await service.restart_channel("tg-assistant")
        release.set()
        await asyncio.wait_for(enabling, timeout=5)
        await asyncio.wait_for(adapter.started.wait(), timeout=5)

        release.clear()
        disabling = asyncio.create_task(service.disable_channel("tg-assistant"))
        await wait_until(lambda: saves[-1] == (False, True))
        # Persist before disrupting the healthy adapter.
        assert service.is_running("tg-assistant")
        with pytest.raises(ChannelError, match="current change of this Channel"):
            await service.update_channel("tg-assistant", observe_unaddressed=True)
        release.set()
        await asyncio.wait_for(disabling, timeout=5)
        await asyncio.wait_for(adapter.stopped.wait(), timeout=5)
        assert storage.get("tg-assistant").enabled is False
        assert service._pending_config_changes == set()
    finally:
        release.set()
        await service.aclose()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["failed-save", "cancelled-save"])
async def test_an_interrupted_update_keeps_config_and_running_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    storage = ChannelStorage(tmp_path)
    original = make_config(enabled=True)
    storage.save(original)
    service = make_service(tmp_path)
    adapters: list[BlockingAdapter] = []
    entered = threading.Event()
    release = threading.Event()
    real_save = service._storage.save

    def create_adapter(_config: Any) -> BlockingAdapter:
        adapters.append(BlockingAdapter())
        return adapters[-1]

    def interrupted_save(config: Any) -> None:
        entered.set()
        if interruption == "failed-save":
            raise OSError("disk full")
        assert release.wait(timeout=5)
        real_save(config)

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    service.start()
    try:
        await asyncio.wait_for(adapters[0].started.wait(), timeout=1)
        monkeypatch.setattr(service._storage, "save", interrupted_save)
        updating = asyncio.create_task(service.update_channel(original.id, enabled=False))
        await wait_until(entered.is_set)
        if interruption == "cancelled-save":
            updating.cancel()
        release.set()
        expected = OSError if interruption == "failed-save" else asyncio.CancelledError
        with pytest.raises(expected):
            await asyncio.wait_for(updating, timeout=5)

        # A failed write changes nothing and a completed one is undone; the
        # healthy adapter was never disturbed.
        assert storage.get(original.id).to_dict() == original.to_dict()
        assert len(adapters) == 1
        assert not adapters[0].stopped.is_set()
        assert service.is_running(original.id)
        assert service._pending_config_changes == set()
    finally:
        release.set()
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_channel_service_aclose_awaits_adapter_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ChannelStorage(tmp_path).save(make_config("tg-enabled", enabled=True))
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
    assert not service.is_running("tg-enabled")
    assert service.has_active_channels() is False
    service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", [True, False], ids=["shutdown-error", "cancellation"])
async def test_stopping_logs_a_failed_adapter_shutdown_but_not_a_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: bool,
) -> None:
    ChannelStorage(tmp_path).save(make_config(enabled=True))
    adapter = ShutdownFailureAdapter() if failing else BlockingAdapter()
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    service.start()
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    with caplog.at_level(logging.ERROR, logger="vbot.channels"):
        await service.aclose()
    service.close()

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    if not failing:
        assert errors == []
        return
    # The stopped task no longer belongs to the service, so the stop path
    # itself logs the failure, with its traceback, or it surfaces nowhere.
    assert [record.getMessage() for record in errors] == [
        "Channel adapter shutdown raised during stop (channel=tg-assistant)"
    ]
    assert errors[0].exc_info is not None
