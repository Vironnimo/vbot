"""Telegram migrations sharing configuration ownership with lifecycle changes."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from core.channels import (
    ChannelAdapter,
    ChannelConfig,
    ChannelConfigError,
    ChannelError,
    ChannelStorage,
)
from tests.core.channels.channels_helpers import (
    BlockingAdapter,
    make_config,
    make_service,
    wait_until,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


@pytest.mark.asyncio
async def test_a_drained_migration_keeps_access_when_config_no_longer_allows_the_old_chat(
    tmp_path: Path,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=False, allowed_chat_ids=["77"])
    storage.save(config)
    service = make_service(tmp_path)
    try:
        # The old adapter admitted migration before a concurrent edit removed
        # the chat from its allowlist; its stored group access still migrates.
        await service._state.grant_group_admin(config.id, "-500", "owner")
        await asyncio.to_thread(service.record_chat_id_migration, config.id, "-500", "-100500")

        assert storage.get(config.id).allowed_chat_ids == ["77"]
        assert service._state.role_for(config.id, "-500", "owner") == "member"
        assert service._state.role_for(config.id, "-100500", "owner") == "admin"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_migration_refuses_overlapping_config_edits_without_blocking_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=False, allowed_chat_ids=["-500"])
    storage.save(config)
    service = make_service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    real_migrate = service._state.migrate_group_access

    def held_migration(channel_id: str, old: str, new: str) -> None:
        entered.set()
        assert release.wait(timeout=5)
        real_migrate(channel_id, old, new)

    monkeypatch.setattr(service._state, "migrate_group_access", held_migration)
    migrating = asyncio.create_task(
        asyncio.to_thread(service.record_chat_id_migration, config.id, "-500", "-100500")
    )
    try:
        await wait_until(entered.is_set)
        with pytest.raises(ChannelError):
            await service.update_channel(config.id, response_mode="all")
        with pytest.raises(ChannelError):
            await service.delete_channel(config.id)
        release.set()
        await asyncio.wait_for(migrating, timeout=5)

        await service.update_channel(config.id, response_mode="all")
        saved = storage.get(config.id)
        assert saved.allowed_chat_ids == ["-100500"]
        assert saved.response_mode == "all"
    finally:
        release.set()
        await asyncio.gather(migrating, return_exceptions=True)
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_migration_waits_for_an_in_flight_disable_and_keeps_it_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True, allowed_chat_ids=["-500"])
    storage.save(config)
    service = make_service(tmp_path)
    loaded = asyncio.Event()
    release = asyncio.Event()
    migration_entered = threading.Event()
    real_load = service._load_config

    async def held_load(channel_id: str) -> ChannelConfig:
        result = await real_load(channel_id)
        loaded.set()
        await release.wait()
        return result

    def migrate() -> None:
        migration_entered.set()
        service.record_chat_id_migration(config.id, "-500", "-100500")

    monkeypatch.setattr(service, "_load_config", held_load)
    disabling = asyncio.create_task(service.disable_channel(config.id))
    migrating: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(loaded.wait(), timeout=5)
        migrating = asyncio.create_task(asyncio.to_thread(migrate))
        await wait_until(migration_entered.is_set)
        release.set()
        await asyncio.wait_for(asyncio.gather(disabling, migrating), timeout=5)

        saved = storage.get(config.id)
        assert saved.enabled is False
        assert saved.allowed_chat_ids == ["-100500"]
    finally:
        release.set()
        await asyncio.gather(disabling, *([migrating] if migrating else []), return_exceptions=True)
        await service.aclose()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    ["restart", "platform-change", "failed-platform-change", "cancelled-platform-change"],
)
async def test_adapter_replacement_drains_migration_before_loading_current_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    change_platform = replacement != "restart"
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True, allowed_chat_ids=["-500"])
    storage.save(config)
    service = make_service(tmp_path)
    await service._state.grant_group_admin(config.id, "-500", "owner")
    stopping = asyncio.Event()
    release = asyncio.Event()
    migrated = asyncio.Event()
    drain_release = asyncio.Event()
    adapters: list[BlockingAdapter] = []
    built_from: list[ChannelConfig] = []

    class MigratingAdapter(BlockingAdapter):
        async def stop(self) -> None:
            stopping.set()
            await release.wait()
            await asyncio.to_thread(service.record_chat_id_migration, config.id, "-500", "-100500")
            migrated.set()
            if replacement == "cancelled-platform-change":
                await drain_release.wait()
            await super().stop()

    def create_adapter(current: ChannelConfig) -> ChannelAdapter:
        if replacement == "failed-platform-change" and current.platform == "discord":
            raise ChannelConfigError("replacement unavailable")
        built_from.append(current)
        adapters.append(BlockingAdapter() if adapters else MigratingAdapter())
        return adapters[-1]

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    service.start()
    updating: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(adapters[0].started.wait(), timeout=5)
        fields = {"platform": "discord"} if change_platform else {"response_mode": "all"}
        updating = asyncio.create_task(service.update_channel(config.id, **fields))
        await asyncio.wait_for(stopping.wait(), timeout=5)
        if not change_platform:
            await asyncio.wait_for(updating, timeout=5)
            # A restart while shutdown is pending coalesces with the update's
            # replacement; it does not require constructing a third adapter.
            assert await service.restart_channel(config.id)
        release.set()
        if replacement == "cancelled-platform-change":
            # The migration is journaled, but the adapter has not finished
            # draining, so channels.db still retains Telegram's group access.
            await asyncio.wait_for(migrated.wait(), timeout=5)
            updating.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(updating, timeout=5)
            drain_release.set()
        elif replacement == "failed-platform-change":
            with pytest.raises(ChannelConfigError):
                await asyncio.wait_for(updating, timeout=5)
        else:
            await asyncio.wait_for(updating, timeout=5)
        await wait_until(lambda: len(adapters) == 2)
        await asyncio.wait_for(adapters[1].started.wait(), timeout=5)

        saved = storage.get(config.id)
        assert built_from[1].to_dict() == saved.to_dict()
        if replacement in {"failed-platform-change", "cancelled-platform-change"}:
            assert saved.platform == "telegram"
            assert saved.allowed_chat_ids == ["-100500"]
            assert saved.response_mode == config.response_mode
            if replacement == "cancelled-platform-change":
                assert service._state.role_for(config.id, "-500", "owner") == "member"
                assert service._state.role_for(config.id, "-100500", "owner") == "admin"
        elif change_platform:
            assert saved.platform == "discord"
            assert saved.allowed_chat_ids == ["-500"]
            assert service._state.role_for(config.id, "-100500", "owner") == "member"
        else:
            assert saved.response_mode == "all"
            assert saved.allowed_chat_ids == ["-100500"]
            assert service._state.role_for(config.id, "-100500", "owner") == "admin"
        assert len(adapters) == 2
        assert service._rollback_chat_migrations == {}
    finally:
        release.set()
        drain_release.set()
        if updating is not None:
            await asyncio.gather(updating, return_exceptions=True)
        await service.aclose()
        service.close()
