"""Channels: Channel state across platform, token and bot changes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from core.channels import (
    ChannelAdapter,
    ChannelConfig,
    ChannelConfigError,
    ChannelService,
    ChannelStorage,
)
from core.channels.adapter import RunButtonBinding
from core.channels.state import ChannelStateStore
from core.utils.timestamps import utc_now_timestamp
from tests.core.channels.channels_helpers import (
    BlockingAdapter,
    DelayedStopAdapter,
    make_config,
    make_service,
    wait_until,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_CHANNEL = "tg-assistant"
_BOT = 7001
_MAIN = "ch-tg-assistant-main"
_STATE_TABLES = (
    "channel_admins",
    "channel_participants",
    "channel_conversations",
    "channel_run_buttons",
    "channel_received",
    "channel_polling",
)
_FILLED = {**dict.fromkeys(_STATE_TABLES, 1), "channel_conversations": 2}
# After a platform change only the shared direct conversation keeps its pointer.
_RESET = {**dict.fromkeys(_STATE_TABLES, 0), "channel_conversations": 1}
_TO_DISCORD: dict[str, Any] = {
    "platform": "discord",
    "token_env_var": "DISCORD_BOT_TOKEN_DC_ASSISTANT",
}


class LateWriteAdapter(DelayedStopAdapter):
    """Writes Channel state while stopping, like a drained platform update."""

    def __init__(self, *, state: ChannelStateStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state

    async def stop(self) -> None:
        await super().stop()
        await self._state.snapshot_participant_role(_CHANNEL, "-100", "60", "Late")


async def _fill(service: ChannelService) -> None:
    state = service._state
    await state.snapshot_participant_role(_CHANNEL, "-100", "50", "Alice")
    await state.set_self_user_id(_CHANNEL, "50")
    await state.grant_group_admin(_CHANNEL, "-100", "51")
    await state.record_received(_CHANNEL, "-100:1")
    state.point_conversation(_CHANNEL, "ch-tg-assistant--100", "group", "ses_group")
    state.point_conversation(_CHANNEL, _MAIN, "direct", "ses_main")
    state.save_update_offset(_CHANNEL, _BOT, 42)
    state.save_run_button_binding(
        _CHANNEL,
        RunButtonBinding(
            id="binding",
            platform_target="-100",
            thread_id=None,
            origin_session_id="origin",
            original_button_data=("run:yes",),
            created_at=utc_now_timestamp(),
        ),
    )


def _state_rows(service: ChannelService) -> dict[str, int]:
    with service.database.read() as connection:
        return {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE channel_id = ?", (_CHANNEL,)
                ).fetchone()[0]
            )
            for table in _STATE_TABLES
        }


def _recorded_platform(service: ChannelService) -> str | None:
    with service.database.read() as connection:
        row = connection.execute(
            "SELECT platform FROM channels WHERE channel_id = ?", (_CHANNEL,)
        ).fetchone()
    return None if row[0] is None else str(row[0])


async def _assert_reset(service: ChannelService) -> None:
    assert _state_rows(service) == _RESET
    assert service._state.active_session_id(_CHANNEL, _MAIN) == "ses_main"
    assert await service.channel_access(_CHANNEL) == {
        "channel_id": _CHANNEL,
        "self_user_id": None,
        "groups": [],
    }
    assert service._state.role_for(_CHANNEL, "-100", "51") == "member"


@pytest.mark.asyncio
async def test_a_platform_change_resets_channel_state_after_the_old_adapter_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ChannelStorage(tmp_path).save(make_config(enabled=True))
    service = make_service(tmp_path)
    await _fill(service)
    stop_gate = asyncio.Event()
    events: list[str] = []
    created: list[LateWriteAdapter] = []

    def create_adapter(config: ChannelConfig) -> ChannelAdapter:
        created.append(
            LateWriteAdapter(
                state=service._state, label=config.platform, stop_gate=stop_gate, events=events
            )
        )
        return created[-1]

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    service.start()
    try:
        await asyncio.wait_for(created[0].started.wait(), timeout=1)

        updating = asyncio.create_task(service.update_channel(_CHANNEL, **_TO_DISCORD))
        await wait_until(lambda: "stop:telegram:begin" in events)
        # The state waits for the old adapter to finish stopping.
        assert not updating.done()
        assert _recorded_platform(service) == "telegram"
        assert _state_rows(service) == _FILLED

        stop_gate.set()
        await asyncio.wait_for(updating, timeout=5)
        await wait_until(lambda: "start:discord" in events)

        assert events.index("stop:telegram:end") < events.index("start:discord")
        assert _recorded_platform(service) == "discord"
        # The old adapter's last write landed before the reset, so it is gone too.
        await _assert_reset(service)
    finally:
        stop_gate.set()
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_a_disabled_channel_changes_platform_without_an_adapter(tmp_path: Path) -> None:
    ChannelStorage(tmp_path).save(make_config(enabled=False))
    service = make_service(tmp_path)
    try:
        await _fill(service)

        await service.update_channel(_CHANNEL, **_TO_DISCORD)

        assert _recorded_platform(service) == "discord"
        await _assert_reset(service)
        assert not service.is_running(_CHANNEL)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_token_and_bot_changes_keep_channel_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ChannelStorage(tmp_path).save(make_config(enabled=True))
    service = make_service(tmp_path)
    await _fill(service)
    adapters: list[BlockingAdapter] = []

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        adapters.append(BlockingAdapter())
        return adapters[-1]

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    service.start()
    try:
        await asyncio.wait_for(adapters[0].started.wait(), timeout=1)

        await service.update_channel(_CHANNEL, token_env_var="TELEGRAM_BOT_TOKEN_OTHER")
        # Rebuilding is deferred until shutdown finishes. Restarting sooner can
        # legitimately coalesce with that pending start instead of creating a
        # third adapter, so observe the updated connection before restarting it.
        await wait_until(lambda: len(adapters) == 2)
        await asyncio.wait_for(adapters[1].started.wait(), timeout=1)
        assert await service.restart_channel(_CHANNEL) is True
        await wait_until(lambda: len(adapters) == 3)

        # Chat and user ids stay valid for another bot of the same platform; the
        # watermark is kept per bot, so a new bot reads none.
        assert _state_rows(service) == _FILLED
        assert _recorded_platform(service) == "telegram"
        assert service._state.load_update_offset(_CHANNEL, _BOT) == 42
        assert service._state.role_for(_CHANNEL, "-100", "51") == "admin"
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_a_failed_platform_change_binds_the_state_back_to_the_previous_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = ChannelStorage(tmp_path)
    original = make_config(enabled=True)
    storage.save(original)
    service = make_service(tmp_path)
    await _fill(service)
    adapters: list[BlockingAdapter] = []

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        adapters.append(BlockingAdapter())
        return adapters[-1]

    real_start_channel = service.start_channel

    def start_channel(
        channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        if config_override is not None and config_override.platform == "discord":
            raise ChannelConfigError("start failed")
        real_start_channel(channel_id, reset_backoff=reset_backoff, config_override=config_override)

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    service.start()
    try:
        await asyncio.wait_for(adapters[0].started.wait(), timeout=1)
        monkeypatch.setattr(service, "start_channel", start_channel)

        with pytest.raises(ChannelConfigError, match="start failed"):
            await service.update_channel(_CHANNEL, **_TO_DISCORD)

        await wait_until(lambda: len(adapters) == 2)
        await asyncio.wait_for(adapters[1].started.wait(), timeout=1)
        assert storage.get(_CHANNEL).to_dict() == original.to_dict()
        # The state follows the restored config; what the change reset stays reset.
        assert _recorded_platform(service) == "telegram"
        await _assert_reset(service)
        assert service._pending_config_changes == set()
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_startup_resets_state_recorded_for_another_platform(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = ChannelStorage(tmp_path)
    storage.save(make_config(enabled=False))
    service = make_service(tmp_path)
    try:
        await _fill(service)
    finally:
        service.close()
    # A hand-edited config, or a platform change interrupted before channels.db.
    storage.save(make_config(enabled=False, platform="discord"))

    with caplog.at_level(logging.INFO):
        restarted = make_service(tmp_path)
    try:
        assert _recorded_platform(restarted) == "discord"
        await _assert_reset(restarted)
        assert "Channel state reset for a new platform (channel=tg-assistant)" in caplog.text
    finally:
        restarted.close()
