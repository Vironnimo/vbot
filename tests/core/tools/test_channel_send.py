"""Tests for the channel_send tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from core.channels.channels import ChannelNotFoundError
from core.tools.channel import (
    CHANNEL_SEND_TOOL_NAME,
    register_channel_send_tool,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope, tool_failure


def make_context(workspace: Path, tool_name: str = CHANNEL_SEND_TOOL_NAME) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        app_root=workspace.parent,
        data_root=workspace.parent / "data",
    )


async def dispatch(
    registry: ToolRegistry,
    workspace: Path,
    arguments: dict[str, object],
) -> dict[str, object]:
    return await registry.dispatch(
        make_context(workspace),
        arguments,
        [CHANNEL_SEND_TOOL_NAME],
    )


def assert_success_envelope(result: dict[str, object]) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert set(data) == {"channel_id", "platform_target"}
    return data


def test_channel_send_happy_path_with_explicit_platform_target(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    chat_sessions = Mock()
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-assistant",
                "message": "Task finished",
                "platform_target": "12345",
            },
        )
    )

    data = assert_success_envelope(result)
    assert data == {"channel_id": "tg-assistant", "platform_target": "12345"}
    channel_service.send.assert_awaited_once_with("tg-assistant", "Task finished", "12345")
    chat_sessions.get_metadata.assert_not_called()


def test_channel_send_resolves_platform_target_from_session_metadata(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    chat_sessions = Mock()
    chat_sessions.get_metadata.return_value = {
        "last_reply_target": {
            "channel_id": "tg-assistant",
            "platform_target": "12345",
        }
    }
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-assistant",
                "message": "Task finished",
            },
        )
    )

    data = assert_success_envelope(result)
    assert data == {"channel_id": "tg-assistant", "platform_target": "12345"}
    chat_sessions.get_metadata.assert_called_once_with("agent-1", "session-1")
    channel_service.send.assert_awaited_once_with("tg-assistant", "Task finished", "12345")


def test_channel_send_fails_when_platform_target_is_missing_everywhere(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    chat_sessions = Mock()
    chat_sessions.get_metadata.return_value = {}
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-assistant",
                "message": "Task finished",
            },
        )
    )

    assert result == tool_failure(
        "invalid_arguments",
        "platform_target is required when session metadata has no "
        "last_reply_target.platform_target",
    )
    channel_service.send.assert_not_called()


def test_channel_send_unknown_channel_returns_failure_envelope(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.send.side_effect = ChannelNotFoundError("Channel not active: tg-missing")
    chat_sessions = Mock()
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-missing",
                "message": "Task finished",
                "platform_target": "12345",
            },
        )
    )

    assert result == tool_failure("channel_not_found", "Channel not active: tg-missing")
    chat_sessions.get_metadata.assert_not_called()


def test_channel_send_disabled_channel_returns_failure_envelope(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.send.side_effect = ChannelNotFoundError("Channel not active: tg-disabled")
    chat_sessions = Mock()
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-disabled",
                "message": "Task finished",
                "platform_target": "12345",
            },
        )
    )

    assert result == tool_failure("channel_not_found", "Channel not active: tg-disabled")
    chat_sessions.get_metadata.assert_not_called()
