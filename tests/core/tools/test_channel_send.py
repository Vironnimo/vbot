"""Tests for the channel_send tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from core.channels.adapter import FileData, RouteFacts
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


def make_channel_config(
    *,
    channel_id: str = "tg-assistant",
    agent_id: str = "agent-1",
    allowed_chat_ids: list[int] | None = None,
) -> Mock:
    return Mock(
        id=channel_id,
        agent_id=agent_id,
        allowed_chat_ids=allowed_chat_ids or [],
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
    channel_service.list_channels.return_value = [make_channel_config()]
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
    channel_service.send.assert_awaited_once_with(
        "tg-assistant",
        "Task finished",
        "12345",
        files=None,
    )
    chat_sessions.get_metadata.assert_not_called()
    channel_service.list_channels.assert_called_once_with()


def test_channel_send_records_outbound_note_in_target_session(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    channel_service.ensure_outbound_session.return_value = RouteFacts(
        agent_id="agent-1", session_id="ch-tg-assistant-12345"
    )
    chat_sessions = Mock()
    session = Mock()
    chat_sessions.get_or_create.return_value = session
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

    assert_success_envelope(result)
    channel_service.ensure_outbound_session.assert_called_once_with("tg-assistant", "12345")
    chat_sessions.get_or_create.assert_called_once_with("agent-1", "ch-tg-assistant-12345")
    session.add_note.assert_called_once()
    note = session.add_note.call_args.args[0]
    assert "channel_send tool" in note
    assert 'by agent "agent-1"' in note
    assert "Task finished" in note


def test_channel_send_outbound_note_lists_attached_file_names(tmp_path: Path) -> None:
    attachment_path = tmp_path / "report.pdf"
    attachment_path.write_bytes(b"%PDF-1.7\n")

    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    channel_service.ensure_outbound_session.return_value = RouteFacts(
        agent_id="agent-1", session_id="ch-tg-assistant-12345"
    )
    chat_sessions = Mock()
    session = Mock()
    chat_sessions.get_or_create.return_value = session
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-assistant",
                "platform_target": "12345",
                "file_paths": [str(attachment_path)],
            },
        )
    )

    assert_success_envelope(result)
    note = session.add_note.call_args.args[0]
    assert "channel_send tool" in note
    assert "Attached file(s): report.pdf" in note


def test_channel_send_succeeds_even_when_note_recording_fails(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    channel_service.ensure_outbound_session.side_effect = RuntimeError("boom")
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
    channel_service.send.assert_awaited_once()


def test_channel_send_resolves_platform_target_from_session_metadata(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
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
    channel_service.send.assert_awaited_once_with(
        "tg-assistant",
        "Task finished",
        "12345",
        files=None,
    )
    channel_service.list_channels.assert_called_once_with()


def test_channel_send_ignores_session_metadata_for_other_channel(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [
        make_channel_config(channel_id="tg-private", allowed_chat_ids=[8506476339])
    ]
    chat_sessions = Mock()
    chat_sessions.get_metadata.return_value = {
        "last_reply_target": {
            "channel_id": "tg-other",
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
                "channel_id": "tg-private",
                "message": "Task finished",
            },
        )
    )

    data = assert_success_envelope(result)
    assert data == {"channel_id": "tg-private", "platform_target": "8506476339"}
    channel_service.send.assert_awaited_once_with(
        "tg-private",
        "Task finished",
        "8506476339",
        files=None,
    )


def test_channel_send_resolves_platform_target_from_unique_allowed_chat_id(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [
        make_channel_config(channel_id="tg-private", allowed_chat_ids=[8506476339]),
    ]
    chat_sessions = Mock()
    chat_sessions.get_metadata.return_value = {}
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-private",
                "message": "Task finished",
            },
        )
    )

    data = assert_success_envelope(result)
    assert data == {"channel_id": "tg-private", "platform_target": "8506476339"}
    chat_sessions.get_metadata.assert_called_once_with("agent-1", "session-1")
    channel_service.list_channels.assert_called_once_with()
    channel_service.send.assert_awaited_once_with(
        "tg-private",
        "Task finished",
        "8506476339",
        files=None,
    )


def test_channel_send_requires_message_or_file_paths(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = []
    chat_sessions = Mock()
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-assistant",
                "platform_target": "12345",
            },
        )
    )

    assert result == tool_failure(
        "invalid_arguments",
        "at least one of message or file_paths must be provided",
    )
    channel_service.send.assert_not_called()


def test_channel_send_file_paths_only_forwards_files(tmp_path: Path) -> None:
    attachment_path = tmp_path / "note.txt"
    attachment_path.write_text("hello", encoding="utf-8")

    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = Mock()
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-assistant",
                "platform_target": "12345",
                "file_paths": [str(attachment_path)],
            },
        )
    )

    data = assert_success_envelope(result)
    assert data == {"channel_id": "tg-assistant", "platform_target": "12345"}
    send_call = channel_service.send.await_args
    assert send_call.args == ("tg-assistant", None, "12345")
    files = send_call.kwargs.get("files")
    assert isinstance(files, list)
    assert len(files) == 1
    assert isinstance(files[0], FileData)
    assert files[0].filename == "note.txt"
    assert files[0].media_type == "text/plain"
    assert files[0].data == b"hello"


def test_channel_send_message_and_file_paths_forwarded(tmp_path: Path) -> None:
    attachment_path = tmp_path / "image.png"
    attachment_path.write_bytes(b"\x89PNG\r\n\x1a\nDATA")

    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = Mock()
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-assistant",
                "message": "caption",
                "platform_target": "12345",
                "file_paths": [str(attachment_path)],
            },
        )
    )

    data = assert_success_envelope(result)
    assert data == {"channel_id": "tg-assistant", "platform_target": "12345"}
    send_call = channel_service.send.await_args
    assert send_call.args == ("tg-assistant", "caption", "12345")
    files = send_call.kwargs.get("files")
    assert isinstance(files, list)
    assert len(files) == 1
    assert files[0].filename == "image.png"
    assert files[0].media_type == "image/png"


def test_channel_send_nonexistent_file_path_returns_failure(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = []
    chat_sessions = Mock()
    registry = ToolRegistry()
    register_channel_send_tool(registry, channel_service, chat_sessions)

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "tg-assistant",
                "platform_target": "12345",
                "file_paths": ["missing.pdf"],
            },
        )
    )

    assert result == tool_failure(
        "invalid_arguments",
        "file_paths[0] is not a file: missing.pdf",
    )
    channel_service.send.assert_not_called()


def test_channel_send_fails_when_platform_target_is_missing_everywhere(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
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
        "last_reply_target.platform_target and the channel has no unique "
        "allowed_chat_ids target",
    )
    channel_service.send.assert_not_called()


def test_channel_send_unknown_channel_returns_failure_envelope(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = []
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

    assert result == tool_failure("channel_not_found", "Channel not found: tg-missing")
    chat_sessions.get_metadata.assert_not_called()


def test_channel_send_rejects_channel_owned_by_other_agent(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config(agent_id="agent-2")]
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

    assert result == tool_failure(
        "invalid_arguments",
        "Channel tg-assistant belongs to agent agent-2, not agent-1",
    )
    channel_service.send.assert_not_called()
    chat_sessions.get_metadata.assert_not_called()


def test_channel_send_disabled_channel_returns_failure_envelope(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.send.side_effect = ChannelNotFoundError("Channel not active: tg-disabled")
    channel_service.list_channels.return_value = [make_channel_config(channel_id="tg-disabled")]
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
