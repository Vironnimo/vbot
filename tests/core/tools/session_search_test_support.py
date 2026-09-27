"""Shared fixtures and production-style dispatch for the session_search Tool tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.sessions import ChatSessionManager
from core.tools.session_search import SESSION_SEARCH_TOOL_NAME, register_session_search_tool
from core.tools.tools import ToolContext, ToolRegistry, is_tool_result_envelope
from tests.core.tools.tools_test_support import dispatch_as_executor

JsonObject = dict[str, Any]


def _context(data_root: Path, *, project_id: str | None) -> ToolContext:
    workspace = data_root / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolContext(
        agent_id="coder",
        session_id="current-session",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=SESSION_SEARCH_TOOL_NAME,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=data_root.parent,
        data_root=data_root,
        project_id=project_id,
    )


async def search(
    data_root: Path,
    arguments: Any,
    backend: Any,
    *,
    sessions: ChatSessionManager | None = None,
    timezone: str | None = None,
    project_id: str | None = None,
) -> JsonObject:
    """Call session_search over ``backend`` as the Tool executor does.

    The caller is Agent ``coder`` in Session ``current-session``; ``timezone``
    is the configured timezone that reads periods given without an offset.
    """
    registry = ToolRegistry()
    register_session_search_tool(
        registry,
        backend,
        sessions,
        timezone_name_loader=(lambda: timezone) if timezone is not None else None,
    )
    return await dispatch_as_executor(
        registry, _context(data_root, project_id=project_id), arguments
    )


def timestamp(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, tzinfo=UTC)


def success(result: JsonObject) -> JsonObject:
    assert is_tool_result_envelope(result)
    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    return data


def failure(result: JsonObject, code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result)
    assert result["ok"] is False
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    return error  # type: ignore[return-value]
