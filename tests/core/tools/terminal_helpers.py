"""Shared fixtures for terminal Tool tests: a fake-PTY manager and Agent calls."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import cast

import pytest_asyncio

from core.projects import ProjectStore
from core.tools.terminal import TERMINAL_TOOL_NAME, register_terminal_tool
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure
from tests.core.tools.terminal_manager_helpers import AdapterFactory


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[tuple[TerminalManager, AdapterFactory]]:
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
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
) -> ToolContext:
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
    ``invalid_arguments`` result the Agent reads.
    """
    registry = ToolRegistry()
    register_terminal_tool(
        registry, manager, projects if projects is not None else ProjectStore(context.data_root)
    )
    try:
        return cast(JsonObject, await registry.dispatch(context, arguments, [TERMINAL_TOOL_NAME]))
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error))
