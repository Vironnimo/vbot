"""Shared fixtures and fakes for edit behavior tests."""

from __future__ import annotations

from pathlib import Path

from core.tools.change_tracker import ChangeTracker
from core.tools.edit import (
    EDIT_TOOL_NAME,
)
from core.tools.tools import ToolContext, is_tool_result_envelope


def make_context(
    workspace: Path,
    tool_name: str = EDIT_TOOL_NAME,
    *,
    cwd: Path | None = None,
    session_id: str = "session-1",
    change_tracker: ChangeTracker | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        session_id=session_id,
        run_id="run-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent / "data",
        cwd=cwd,
        change_tracker=change_tracker,
    )


def assert_success_envelope(result: dict[str, object]) -> dict[str, object]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is True
    assert result["error"] is None
    assert result["artifacts"] == []
    data = result["data"]
    assert isinstance(data, dict)
    assert set(data) == {
        "path",
        "first_changed_line",
        "last_changed_line",
        "replacements",
        "preview",
    }
    return data


def assert_failure_envelope(result: dict[str, object], code: str) -> dict[str, str]:
    assert is_tool_result_envelope(result) is True
    assert result["ok"] is False
    assert result["data"] is None
    assert result["artifacts"] == []
    error = result["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]
    return error  # type: ignore[return-value]
