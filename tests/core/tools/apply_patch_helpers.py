"""Shared fixtures and fakes for apply patch behavior tests."""

from __future__ import annotations

from pathlib import Path

from core.tools.apply_patch import make_apply_patch_handler
from core.tools.file_state import FileReadState
from core.tools.tools import ToolContext, is_tool_result_envelope


def context(root: Path, **kwargs) -> ToolContext:
    return ToolContext(
        agent_id="agent-test",
        session_id="session-test",
        run_id="run-test",
        tool_call_id="call-test",
        tool_name="apply_patch",
        tool_call_index=0,
        workspace=root,
        data_root=root / "data",
        vbot_root=root,
        **kwargs,
    )


def apply(root: Path, patch: str, *, state=None, ctx=None):
    result = make_apply_patch_handler(state or FileReadState())(
        ctx or context(root), {"patch": patch}
    )
    assert isinstance(result, dict)
    assert is_tool_result_envelope(result)
    return result


def update(body: str, path: str = "file.txt") -> str:
    return f"*** Begin Patch\n*** Update File: {path}\n{body}\n*** End Patch"
