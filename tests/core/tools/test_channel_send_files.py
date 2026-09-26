"""Channel send: files behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from core.channels import ChannelNotFoundError
from core.channels.adapter import FileData
from core.tools.channel import (
    CHANNEL_SEND_TOOL_NAME,
    register_channel_send_tool,
)
from core.tools.tools import (
    ToolRegistry,
    tool_failure,
)
from tests.core.tools.channel_send_helpers import (
    _TEST_MAX_ATTACHMENT_SIZE_BYTES,
    assert_success_envelope,
    dispatch,
    make_channel_config,
    make_chat_sessions,
    make_context,
)


def test_channel_send_requires_message_or_file_paths(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

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
        'channel_send was not run: it needs "message", "file_paths", or both. Send: '
        '{"channel_id":"tg-assistant","platform_target":"12345","message":"<text to send>"}',
    )
    channel_service.send.assert_not_called()


@pytest.mark.parametrize(
    "empty",
    ({"file_paths": []}, {"buttons": []}),
)
def test_channel_send_treats_an_empty_list_as_left_out(
    tmp_path: Path, empty: dict[str, object]
) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        make_chat_sessions(),
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )
    arguments = {"channel_id": "tg-assistant", "platform_target": "12345", "message": "Hello"}

    result = asyncio.run(dispatch(registry, tmp_path, {**arguments, **empty}))

    assert_success_envelope(result)
    channel_service.send.assert_awaited_once_with(
        "tg-assistant", "Hello", "12345", files=None, thread_id=None, buttons=None
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            {"channel_id": "tg-assistant", "message": "Hello", "buttons": [[]]},
            'channel_send was not run: "buttons[0]" must not be empty.',
        ),
    ),
)
def test_channel_send_rejects_explicit_empty_collections(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        make_chat_sessions(),
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

    result = asyncio.run(dispatch(registry, tmp_path, arguments))

    assert result == tool_failure("invalid_arguments", message)
    channel_service.send.assert_not_awaited()


def test_channel_send_file_paths_only_forwards_files(tmp_path: Path) -> None:
    attachment_path = tmp_path / "note.txt"
    attachment_path.write_text("hello", encoding="utf-8")

    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

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


def test_channel_send_relative_file_path_resolves_from_cwd(tmp_path: Path) -> None:
    # In a project session, relative file paths resolve against the project cwd
    # (like every other file-taking tool), not the agent workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("note.txt").write_text("from repo", encoding="utf-8")

    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

    result = asyncio.run(
        registry.dispatch(
            make_context(workspace, cwd=repo),
            {
                "channel_id": "tg-assistant",
                "platform_target": "12345",
                "file_paths": ["note.txt"],
            },
            [CHANNEL_SEND_TOOL_NAME],
        )
    )

    assert_success_envelope(result)
    files = channel_service.send.await_args.kwargs.get("files")
    assert isinstance(files, list)
    assert files[0].data == b"from repo"


def test_channel_send_message_and_file_paths_forwarded(tmp_path: Path) -> None:
    attachment_path = tmp_path / "image.png"
    attachment_path.write_bytes(b"\x89PNG\r\n\x1a\nDATA")

    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

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
    (tmp_path / "missing.pdf.txt").write_text("x", encoding="utf-8")
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

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
        f'channel_send was not run: file_paths "missing.pdf" does not exist '
        f'({tmp_path / "missing.pdf"}). Files with similar names there: "missing.pdf.txt".',
    )
    channel_service.send.assert_not_called()


def test_channel_send_oversize_file_returns_failure_without_reading(tmp_path: Path) -> None:
    attachment_path = tmp_path / "big.bin"
    attachment_path.write_bytes(b"x" * 16)

    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=8,
    )

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

    assert result == tool_failure(
        "invalid_arguments",
        f'channel_send was not run: file_paths "{attachment_path}" is 16 bytes; files '
        "sent through a Channel may be at most 8 bytes.",
    )
    channel_service.send.assert_not_called()


def test_channel_send_fails_when_platform_target_is_missing_everywhere(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    chat_sessions.get_metadata.return_value = {}
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

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
        "channel_send was not run: this conversation is not with a chat on tg-assistant, and "
        "the Channel allows no chats yet, so there is no chat to send to by default. Give the "
        "chat's id on Telegram. Send: "
        '{"channel_id":"tg-assistant","platform_target":"<chat id>","message":"Task finished"}',
    )
    channel_service.send.assert_not_called()


def test_channel_send_unknown_channel_returns_failure_envelope(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = []
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

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
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

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
    chat_sessions = make_chat_sessions()
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

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
