"""Shared fixtures and fakes for session search behavior tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.tools.session_search import (
    SESSION_SEARCH_TOOL_NAME,
)
from core.tools.tools import ToolContext, is_tool_result_envelope

JsonObject = dict[str, Any]


def make_context(
    data_root: Path,
    *,
    agent_id: str = "coder",
    project_id: str | None = None,
    tool_name: str = SESSION_SEARCH_TOOL_NAME,
) -> ToolContext:
    workspace = data_root / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolContext(
        agent_id=agent_id,
        session_id="current-session",
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=data_root.parent,
        data_root=data_root,
        project_id=project_id,
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
