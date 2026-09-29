"""Runtime startup of configured Channels and their ``channel_send`` Tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.agents.agents import AgentStore
from core.channels import ChannelService
from core.runtime._configuration import _resolve_resources_path
from core.runtime.runtime import Runtime
from core.utils.config import Config
from tests.core.channels.channels_test_support import BlockingAdapter


def _write_channel(data_dir: Path, channel_id: str, agent_id: str, token_env_var: str) -> None:
    channel_dir = data_dir / "channels" / channel_id
    channel_dir.mkdir(parents=True, exist_ok=True)
    channel_dir.joinpath("channel.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": channel_id,
                "platform": "telegram",
                "agent_id": agent_id,
                "dm_scope": "per_conversation",
                "allowed_chat_ids": [12345],
                "token_env_var": token_env_var,
                "enabled": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _seed_assistant_channel(config: Config) -> None:
    AgentStore(
        config.data_dir,
        template_dir=_resolve_resources_path(config) / "workspace-templates",
    ).create("assistant", "Assistant")
    _write_channel(config.data_dir, "tg-assistant", "assistant", "TELEGRAM_BOT_TOKEN_TG_ASSISTANT")


@pytest.mark.asyncio
async def test_runtime_start_survives_channels_that_cannot_start(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_assistant_channel(config)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)
    _write_channel(config.data_dir, "tg-orphan", "missing-agent", "TELEGRAM_BOT_TOKEN_TG_ORPHAN")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ORPHAN", "test-token")
    runtime = Runtime(config)

    runtime.start()
    try:
        channels = runtime.channel_service
        assert channels.has_active_channels() is False
        for channel_id, reason in (
            ("tg-assistant", "TELEGRAM_BOT_TOKEN_TG_ASSISTANT"),
            ("tg-orphan", "missing-agent"),
        ):
            assert channels.is_failed(channel_id) is True
            assert reason in (channels.failure_reason(channel_id) or "")
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_start_runs_enabled_channel_adapters_until_stop(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_assistant_channel(config)
    adapter = BlockingAdapter()
    monkeypatch.setattr(ChannelService, "_create_adapter", lambda _service, _config: adapter)
    runtime = Runtime(config)

    runtime.start()
    try:
        await asyncio.wait_for(adapter.started.wait(), timeout=5)
        assert "channel_send" in [tool.name for tool in runtime.tools.list_tools()]
        assert runtime.channel_service.has_active_channels() is True
    finally:
        await runtime.aclose()
    assert adapter.stopped.is_set()


def test_runtime_registers_channel_send_for_enabled_channel_without_running_adapter(
    config: Config,
) -> None:
    _seed_assistant_channel(config)
    runtime = Runtime(config)

    runtime.start()
    try:
        assert "channel_send" in [tool.name for tool in runtime.tools.list_tools()]
        assert runtime.channel_service.has_enabled_channels() is True
        assert runtime.channel_service.has_active_channels() is False
    finally:
        runtime.stop()
