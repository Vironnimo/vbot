"""Shared helpers for apply_patch Tool tests.

``apply`` runs a patch through the Tool handler. ``call`` runs any call shape
through the Tool executor, as a Run does: argument repair and refusals included,
and a refused call comes back as an ``invalid_arguments`` result.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from core.tools.apply_patch import make_apply_patch_handler, register_apply_patch_tool
from core.tools.file_state import FileReadState
from core.tools.read import register_read_tool
from core.tools.tools import (
    ToolCall,
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolRegistry,
    is_tool_result_envelope,
)


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


def registry(state: FileReadState | None = None, *, read: bool = False) -> ToolRegistry:
    """Register apply_patch, and read when a test follows a result into a read call."""
    state = state or FileReadState()
    tools = ToolRegistry()
    register_apply_patch_tool(tools, file_state=state)
    if read:
        register_read_tool(
            tools,
            attachment_store=Mock(),
            speech_service=Mock(),
            file_state=state,
            speech_max_size_bytes=1024,
        )
    return tools


async def call(
    root: Path, arguments: dict, *, tools: ToolRegistry | None = None, name="apply_patch"
):
    """Run one call through the Tool executor and return its result envelope."""
    tools = tools or registry()
    results = await ToolExecutor(tools).execute_many(
        [ToolCall(id="call-test", name=name, arguments=arguments)],
        ToolExecutionConfig(
            agent_id="agent-test",
            session_id="session-test",
            run_id="run-test",
            workspace=root,
            data_root=root / "data",
            vbot_root=root,
            allowed_tools=["apply_patch", "read"],
        ),
    )
    assert is_tool_result_envelope(results[0])
    return results[0]


def update(body: str, path: str = "file.txt") -> str:
    return f"*** Begin Patch\n*** Update File: {path}\n{body}\n*** End Patch"


def text(result: dict) -> str:
    """Return the Model-facing text of an apply_patch result: content or error message."""
    if result["ok"]:
        return str(result["data"]["content"])
    return str(result["error"]["message"])
