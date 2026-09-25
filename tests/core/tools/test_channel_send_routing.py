"""Channel send: routing behavior."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.channels.adapter import RouteFacts
from core.sessions import ChatSessionManager, SessionAddress
from core.tools.channel import (
    register_channel_send_tool,
)
from core.tools.tools import (
    ToolRegistry,
)
from tests.core.tools.channel_send_helpers import (
    _TEST_MAX_ATTACHMENT_SIZE_BYTES,
    assert_success_envelope,
    dispatch,
    make_channel_config,
    make_chat_sessions,
    make_context,
)


def test_channel_send_records_outbound_note_in_target_session(tmp_path: Path) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    channel_service.ensure_outbound_session = AsyncMock(
        return_value=RouteFacts(agent_id="agent-1", session_id="ch-tg-assistant-12345")
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
    channel_service.ensure_outbound_session.assert_awaited_once_with("tg-assistant", "12345")
    chat_sessions.get_or_create.assert_called_once_with(
        SessionAddress(project_id=None, agent_id="agent-1", session_id="ch-tg-assistant-12345")
    )
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
    channel_service.ensure_outbound_session = AsyncMock(
        return_value=RouteFacts(agent_id="agent-1", session_id="ch-tg-assistant-12345")
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
    channel_service.ensure_outbound_session = AsyncMock(side_effect=RuntimeError("boom"))
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
    chat_sessions.get_metadata.assert_called_once_with(
        SessionAddress(project_id=None, agent_id="agent-1", session_id="session-1")
    )
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
    chat_sessions.get_metadata.assert_called_once_with(
        SessionAddress(project_id=None, agent_id="agent-1", session_id="session-1")
    )
    channel_service.list_channels.assert_called_once_with()
    channel_service.send.assert_awaited_once_with(
        "tg-private",
        "Task finished",
        "8506476339",
        files=None,
        thread_id=None,
        buttons=None,
    )


@pytest.mark.usefixtures("current_format_data_directory")
@pytest.mark.parametrize("target_source", ["configured_default", "project_metadata"])
@pytest.mark.asyncio
async def test_project_session_resolves_its_own_implicit_channel_target(
    tmp_path: Path, target_source: str
) -> None:
    sessions = ChatSessionManager(tmp_path)
    project_address = SessionAddress(
        project_id="project-one", agent_id="agent-1", session_id="session-1"
    )
    sessions.create("agent-1", session_id="session-1", project_id="project-one")
    expected_target = "12345"
    if target_source == "project_metadata":
        expected_target = "23456"
        sessions.set_metadata(
            project_address,
            {
                "last_reply_target": {
                    "channel_id": "tg-assistant",
                    "platform_target": expected_target,
                }
            },
        )
        # An identically named Identity Session must never choose the Project
        # Session's destination, even when its Channel id also matches.
        identity = sessions.create("agent-1", session_id="session-1")
        sessions.set_metadata(
            identity.address,
            {
                "last_reply_target": {
                    "channel_id": "tg-assistant",
                    "platform_target": "99999",
                }
            },
        )
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config(allowed_chat_ids=[12345])]
    channel_service.ensure_outbound_session = AsyncMock(
        return_value=RouteFacts(agent_id="agent-1", session_id="outbound-session")
    )
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )
    try:
        result = await registry.dispatch(
            make_context(tmp_path, project_id="project-one"),
            {"channel_id": "tg-assistant", "message": "Project result"},
            ["channel_send"],
        )

        assert assert_success_envelope(result)["platform_target"] == expected_target
        channel_service.send.assert_awaited_once_with(
            "tg-assistant",
            "Project result",
            expected_target,
            files=None,
            thread_id=None,
            buttons=None,
        )
    finally:
        sessions.close()


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


@pytest.mark.usefixtures("current_format_data_directory")
def test_channel_send_session_reads_and_note_run_on_the_session_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_sessions = ChatSessionManager(tmp_path)
    caller = SessionAddress(project_id=None, agent_id="agent-1", session_id="session-1")
    target = SessionAddress(project_id=None, agent_id="agent-1", session_id="ch-tg-assistant-12345")
    chat_sessions.create("agent-1", session_id="session-1")
    chat_sessions.set_metadata(
        caller,
        {"last_reply_target": {"channel_id": "tg-assistant", "platform_target": "12345"}},
    )
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [make_channel_config()]
    channel_service.ensure_outbound_session = AsyncMock(
        return_value=RouteFacts(agent_id="agent-1", session_id=target.session_id)
    )
    threads: dict[str, str] = {}
    get_metadata = chat_sessions.get_metadata
    get_or_create = chat_sessions.get_or_create

    def recorded_metadata(address: SessionAddress) -> Any:
        threads["target"] = threading.current_thread().name
        return get_metadata(address)

    def recorded_get_or_create(address: SessionAddress) -> Any:
        threads["note"] = threading.current_thread().name
        return get_or_create(address)

    monkeypatch.setattr(chat_sessions, "get_metadata", recorded_metadata)
    monkeypatch.setattr(chat_sessions, "get_or_create", recorded_get_or_create)
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        chat_sessions,
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )
    try:
        result = asyncio.run(
            dispatch(registry, tmp_path, {"channel_id": "tg-assistant", "message": "Done"})
        )

        assert assert_success_envelope(result)["platform_target"] == "12345"
        notes = [entry for entry in chat_sessions.get(target).load() if entry.role == "note"]
        assert len(notes) == 1
        # The metadata target and the outbound note use the Session database's pool.
        assert threads["target"].startswith("vbot-db-sessions")
        assert threads["note"].startswith("vbot-db-sessions")
    finally:
        chat_sessions.close()
