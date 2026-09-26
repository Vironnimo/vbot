"""Shared fixtures and fakes for channel send behavior tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

from core.tools.channel import (
    CHANNEL_SEND_TOOL_NAME,
)
from core.tools.tools import (
    ToolContext,
    ToolRegistry,
    is_tool_result_envelope,
    tool_failure,
)

_TEST_MAX_ATTACHMENT_SIZE_BYTES = 20_971_520


class _NullAsyncContext:
    """Stand-in for the per-session write lock in mocked chat-session managers."""

    async def __aenter__(self) -> _NullAsyncContext:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def make_chat_sessions() -> Mock:
    """Return a mock ``ChatSessionManager`` whose ``write_lock`` and async API are usable.

    ``run_async`` runs its function inline and ``get_metadata_async`` delegates to
    ``get_metadata``, so tests configure and assert the synchronous mocks.
    """
    chat_sessions = Mock()
    chat_sessions.write_lock.return_value = _NullAsyncContext()

    def run_inline(function: Any, *arguments: Any, **keyword_arguments: Any) -> Any:
        return function(*arguments, **keyword_arguments)

    chat_sessions.run_async = AsyncMock(side_effect=run_inline)
    chat_sessions.get_metadata_async = AsyncMock(
        side_effect=lambda address: chat_sessions.get_metadata(address)
    )
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
    platform: str = "telegram",
    enabled: bool = True,
    allowed_chat_ids: list[int] | list[str] | None = None,
) -> Mock:
    return Mock(
        id=channel_id,
        agent_id=agent_id,
        platform=platform,
        enabled=enabled,
        allowed_chat_ids=allowed_chat_ids or [],
    )


async def dispatch(
    registry: ToolRegistry,
    workspace: Path,
    arguments: dict[str, object],
) -> dict[str, object]:
    try:
        return await registry.dispatch(
            make_context(workspace),
            arguments,
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
