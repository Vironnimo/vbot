"""Tests for the channel_send tool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from core.channels.adapter import FileData, RouteFacts
from core.channels.channels import ChannelNotFoundError
from core.extensions import InteractionButton
from core.tools.channel import (
    CHANNEL_SEND_TOOL_DESCRIPTION,
    CHANNEL_SEND_TOOL_NAME,
    CHANNEL_SEND_TOOL_PARAMETERS,
    register_channel_send_tool,
)
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope, tool_failure

_TEST_MAX_ATTACHMENT_SIZE_BYTES = 20_971_520


def test_channel_send_agent_guidance_requires_tool_for_channel_files() -> None:
    assert CHANNEL_SEND_TOOL_DESCRIPTION == (
        "Send a proactive message or any file through a configured channel. Always use "
        "this tool for channel file delivery, including replies. Put the complete request "
        "inside request with operation set to send."
    )
    send = CHANNEL_SEND_TOOL_PARAMETERS["properties"]["request"]["anyOf"][0]
    assert send["required"] == ["operation", "channel_id"]
    assert send["properties"]["operation"] == {"type": "string", "enum": ["send"]}
    properties = send["properties"]
    assert isinstance(properties, dict)
    file_paths = properties["file_paths"]
    assert isinstance(file_paths, dict)
    assert file_paths["description"] == (
        "File paths to deliver through the channel. Use for every channel file delivery, "
        "including replies. Relative paths resolve from the working directory."
    )


class _NullAsyncContext:
    """Stand-in for the per-session write lock in mocked chat-session managers."""

    async def __aenter__(self) -> _NullAsyncContext:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def make_chat_sessions() -> Mock:
    """Return a mock ``ChatSessionManager`` whose ``write_lock`` is async-usable."""
    chat_sessions = Mock()
    chat_sessions.write_lock.return_value = _NullAsyncContext()
    return chat_sessions


def make_context(
    workspace: Path,
    tool_name: str = CHANNEL_SEND_TOOL_NAME,
    cwd: Path | None = None,
    project_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id="session-1",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
        cwd=cwd,
        project_id=project_id,
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
    if set(arguments) == {"request"}:
        canonical = arguments
    elif set(arguments) == {"send"} and isinstance(arguments["send"], dict):
        canonical = {
            "request": {
                "operation": "send",
                **arguments["send"],
            }
        }
    else:
        canonical = {"request": {"operation": "send", **arguments}}
    try:
        return await registry.dispatch(
            make_context(workspace),
            canonical,
            [CHANNEL_SEND_TOOL_NAME],
        )
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))


def assert_success_envelope(
    result: dict[str, object],
    *,
    with_thread: bool = False,
) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    expected_keys = {"channel_id", "platform_target"}
    if with_thread:
        expected_keys.add("thread_id")
    assert set(data) == expected_keys
    return data


def test_channel_send_happy_path_with_explicit_platform_target(tmp_path: Path) -> None:
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
        thread_id=None,
        buttons=None,
    )
    chat_sessions.get_metadata.assert_not_called()
    channel_service.list_channels.assert_called_once_with()


def test_channel_send_accepts_canonical_send_operation(tmp_path: Path) -> None:
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
                "send": {
                    "channel_id": "tg-assistant",
                    "message": "Task finished",
                    "platform_target": "12345",
                }
            },
        )
    )

    assert_success_envelope(result)
    channel_service.send.assert_awaited_once()


def test_channel_send_passes_buttons_to_service(tmp_path: Path) -> None:
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
                "message": "Shopping list",
                "platform_target": "12345",
                "buttons": [
                    [
                        {"label": "Milk ⬜", "data": "chk:milk"},
                        {"label": "Eggs ⬜", "data": "chk:eggs"},
                    ]
                ],
            },
        )
    )

    assert_success_envelope(result)
    channel_service.send.assert_awaited_once_with(
        "tg-assistant",
        "Shopping list",
        "12345",
        files=None,
        thread_id=None,
        buttons=[
            [
                InteractionButton(label="Milk ⬜", data="chk:milk"),
                InteractionButton(label="Eggs ⬜", data="chk:eggs"),
            ]
        ],
    )


def test_channel_send_binds_run_buttons_to_calling_session(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    channel_service.ensure_outbound_session.return_value = RouteFacts(
        agent_id="agent-1",
        session_id="telegram-session",
    )
    chat_sessions = make_chat_sessions()
    chat_sessions.get_or_create.return_value = Mock()
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
                "message": "Shopping list",
                "platform_target": "12345",
                "buttons": [[{"label": "Fertig", "data": "run:done"}]],
            },
        )
    )

    assert_success_envelope(result)
    channel_service.send.assert_awaited_once_with(
        "tg-assistant",
        "Shopping list",
        "12345",
        files=None,
        thread_id=None,
        buttons=[[InteractionButton(label="Fertig", data="run:done")]],
        run_origin=RouteFacts(agent_id="agent-1", session_id="session-1"),
    )


def test_project_channel_send_keeps_legacy_unbound_run_button_behavior(tmp_path: Path) -> None:
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
            make_context(tmp_path, project_id="project-1"),
            {
                "request": {
                    "operation": "send",
                    "channel_id": "tg-assistant",
                    "message": "Project approval",
                    "platform_target": "12345",
                    "buttons": [[{"label": "Approve", "data": "run:approve"}]],
                }
            },
            [CHANNEL_SEND_TOOL_NAME],
        )
    )

    assert_success_envelope(result)
    channel_service.send.assert_awaited_once_with(
        "tg-assistant",
        "Project approval",
        "12345",
        files=None,
        thread_id=None,
        buttons=[[InteractionButton(label="Approve", data="run:approve")]],
    )


def test_channel_send_rejects_malformed_buttons(tmp_path: Path) -> None:
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
                "message": "Shopping list",
                "platform_target": "12345",
                # Missing the required "data" field.
                "buttons": [[{"label": "Milk"}]],
            },
        )
    )

    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_arguments"
    assert "'data' is a required property" in str(error["message"])
    channel_service.send.assert_not_awaited()


def test_channel_send_rejects_unknown_button_fields(tmp_path: Path) -> None:
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
                "message": "Shopping list",
                "platform_target": "12345",
                "buttons": [[{"label": "Milk", "data": "chk:milk", "unexpected": True}]],
            },
        )
    )

    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_arguments"
    assert "Additional properties are not allowed" in str(error["message"])
    channel_service.send.assert_not_awaited()


def test_channel_send_records_outbound_note_in_target_session(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    channel_service.ensure_outbound_session.return_value = RouteFacts(
        agent_id="agent-1", session_id="ch-tg-assistant-12345"
    )
    chat_sessions = make_chat_sessions()
    session = Mock()
    chat_sessions.get_or_create.return_value = session
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
    chat_sessions = make_chat_sessions()
    session = Mock()
    chat_sessions.get_or_create.return_value = session
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

    assert_success_envelope(result)
    note = session.add_note.call_args.args[0]
    assert "channel_send tool" in note
    assert "Attached file(s): report.pdf" in note


def test_channel_send_succeeds_even_when_note_recording_fails(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    channel_service.ensure_outbound_session.side_effect = RuntimeError("boom")
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

    data = assert_success_envelope(result)
    assert data == {"channel_id": "tg-assistant", "platform_target": "12345"}
    channel_service.send.assert_awaited_once()


def test_channel_send_resolves_platform_target_from_session_metadata(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    chat_sessions.get_metadata.return_value = {
        "last_reply_target": {
            "channel_id": "tg-assistant",
            "platform_target": "12345",
        }
    }
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

    data = assert_success_envelope(result)
    assert data == {"channel_id": "tg-assistant", "platform_target": "12345"}
    chat_sessions.get_metadata.assert_called_once_with("agent-1", "session-1")
    channel_service.send.assert_awaited_once_with(
        "tg-assistant",
        "Task finished",
        "12345",
        files=None,
        thread_id=None,
        buttons=None,
    )
    channel_service.list_channels.assert_called_once_with()


def test_channel_send_ignores_session_metadata_for_other_channel(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [
        make_channel_config(channel_id="tg-private", allowed_chat_ids=[8506476339])
    ]
    chat_sessions = make_chat_sessions()
    chat_sessions.get_metadata.return_value = {
        "last_reply_target": {
            "channel_id": "tg-other",
            "platform_target": "12345",
        }
    }
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
        thread_id=None,
        buttons=None,
    )


def test_channel_send_resolves_platform_target_from_unique_allowed_chat_id(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [
        make_channel_config(channel_id="tg-private", allowed_chat_ids=[8506476339]),
    ]
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
        thread_id=None,
        buttons=None,
    )


def test_channel_send_passes_explicit_thread_id(tmp_path: Path) -> None:
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
                "message": "Task finished",
                "platform_target": "12345",
                "thread_id": "42",
            },
        )
    )

    data = assert_success_envelope(result, with_thread=True)
    assert data == {"channel_id": "tg-assistant", "platform_target": "12345", "thread_id": "42"}
    channel_service.send.assert_awaited_once_with(
        "tg-assistant",
        "Task finished",
        "12345",
        files=None,
        thread_id="42",
        buttons=None,
    )


def test_channel_send_adopts_thread_from_session_metadata(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    chat_sessions = make_chat_sessions()
    chat_sessions.get_metadata.return_value = {
        "last_reply_target": {
            "channel_id": "tg-assistant",
            "platform_target": "12345",
            "thread_id": "42",
        }
    }
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

    # The metadata topic rides along with the metadata target automatically.
    data = assert_success_envelope(result, with_thread=True)
    assert data == {"channel_id": "tg-assistant", "platform_target": "12345", "thread_id": "42"}
    channel_service.send.assert_awaited_once_with(
        "tg-assistant",
        "Task finished",
        "12345",
        files=None,
        thread_id="42",
        buttons=None,
    )


def test_channel_send_requires_message_or_file_paths(tmp_path: Path) -> None:
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
                "channel_id": "tg-assistant",
                "platform_target": "12345",
            },
        )
    )

    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_arguments"
    assert "is a required property" in str(error["message"])
    channel_service.send.assert_not_called()


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
                "request": {
                    "operation": "send",
                    "channel_id": "tg-assistant",
                    "platform_target": "12345",
                    "file_paths": ["note.txt"],
                }
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
        f"file_paths[0] size 16 exceeds limit 8: {attachment_path}",
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
        "platform_target is required when session metadata has no "
        "last_reply_target.platform_target and the channel has no unique "
        "allowed_chat_ids target",
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
