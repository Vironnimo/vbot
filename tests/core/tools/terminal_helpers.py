"""Shared fixtures for terminal Tool tests: a fake-PTY manager and Agent calls."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Collection
from pathlib import Path

import pytest_asyncio

from core.projects import ProjectStore
from core.tools.terminal import TERMINAL_TOOL_NAME, register_terminal_tool
from core.tools.terminal_manager import TerminalManager, TerminalRenderHost
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure_for_exception
from tests.core.tools.terminal_manager_helpers import AdapterFactory


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[tuple[TerminalManager, AdapterFactory]]:
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        yield manager, factory
    finally:
        await manager.aclose()


def make_context(
    tmp_path: Path,
    *,
    session_id: str = "session-a",
    result_persisted_hook: Callable[[Callable[[], None]], None] | None = None,
    nesting_depth: int = 0,
    offered_tools: Collection[str] | None = None,
) -> ToolContext:
    """A terminal call's context; *offered_tools* are the Tools the Model was shown."""
    return ToolContext(
        agent_id="agent-a",
        session_id=session_id,
        run_id="run-a",
        tool_call_id="call-a",
        tool_name=TERMINAL_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        cwd=tmp_path,
        project_id="project-a",
        result_persisted_hook=result_persisted_hook,
        nesting_depth=nesting_depth,
        offered_tools=offered_tools,
    )


async def call(
    manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
    projects: ProjectStore | None = None,
) -> JsonObject:
    """Run one Agent call the way the Tool executor does.

    The registered Tool normalizes other harnesses' spellings and validates the
    contract before the handler runs; the executor turns a refused call into the
    ``invalid_arguments`` result the Agent reads. The Run may call the Tools the
    context offers besides terminal.
    """
    registry = ToolRegistry()
    register_terminal_tool(
        registry, manager, projects if projects is not None else ProjectStore(context.data_root)
    )
    allowed = sorted({TERMINAL_TOOL_NAME, *(context.offered_tools or ())})
    try:
        return await registry.dispatch(context, arguments, allowed)
    except Exception as error:
        return tool_failure_for_exception(context.tool_name, error)


def details(
    manager: TerminalManager, tmp_path: Path, arguments: JsonObject, result: JsonObject
) -> list[JsonObject]:
    """Return the detail blocks the user sees for one terminal call."""
    registry = ToolRegistry()
    register_terminal_tool(registry, manager, ProjectStore(tmp_path))
    shown: list[JsonObject] = registry.display_for_call(
        TERMINAL_TOOL_NAME, arguments, result=result
    )["details"]
    return shown
