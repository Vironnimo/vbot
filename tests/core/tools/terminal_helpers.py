"""Shared fixtures and fakes for terminal behavior tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import cast

import pytest_asyncio

from core.projects import ProjectStore
from core.tools.terminal import (
    TERMINAL_TOOL_NAME,
    make_terminal_handler,
)
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import JsonObject, ToolContext
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
    project_store = projects if projects is not None else ProjectStore(context.data_root)
    return cast(
        JsonObject,
        await make_terminal_handler(manager, project_store)(context, arguments),
    )
