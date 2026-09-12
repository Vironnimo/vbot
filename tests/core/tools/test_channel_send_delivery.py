"""Channel send: delivery behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from core.channels.adapter import RouteFacts
from core.extensions import InteractionButton
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


def test_channel_send_rejects_retired_envelope_shapes(tmp_path: Path) -> None:
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

    retired_shapes: tuple[dict[str, object], ...] = (
        {
            "request": {
                "operation": "send",
                "channel_id": "tg-assistant",
                "message": "Task finished",
                "platform_target": "12345",
            }
        },
        {
            "send": {
                "channel_id": "tg-assistant",
                "message": "Task finished",
                "platform_target": "12345",
            }
        },
    )
    for retired_arguments in retired_shapes:
        result = asyncio.run(
            dispatch(
                registry,
                tmp_path,
                retired_arguments,
            )
        )
        assert result["ok"] is False
        error = result["error"]
        assert isinstance(error, dict)
        assert error["code"] == "invalid_arguments"

    channel_service.send.assert_not_awaited()


def test_channel_send_accepts_flat_arguments(tmp_path: Path) -> None:
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
                "channel_id": "tg-assistant",
                "message": "Project approval",
                "platform_target": "12345",
                "buttons": [[{"label": "Approve", "data": "run:approve"}]],
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
    channel_service.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("thread_id", "42"),
        ("buttons", [[{"label": "Go", "data": "run:go"}]]),
    ),
)
def test_channel_send_rejects_telegram_only_fields_for_discord(
    tmp_path: Path,
    field_name: str,
    value: object,
) -> None:
    channel_service = Mock()
    channel_service.send = AsyncMock()
    channel_service.list_channels.return_value = [
        make_channel_config(channel_id="discord-primary", platform="discord")
    ]
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        make_chat_sessions(),
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

    result = asyncio.run(
        dispatch(
            registry,
            tmp_path,
            {
                "channel_id": "discord-primary",
                "message": "Hello",
                "platform_target": "12345",
                field_name: value,
            },
        )
    )

    assert result == tool_failure(
        "invalid_arguments",
        f"{field_name} not supported by discord Channel discord-primary",
    )
    channel_service.send.assert_not_awaited()
