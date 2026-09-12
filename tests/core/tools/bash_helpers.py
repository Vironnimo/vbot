"""Shared fixtures and fakes for bash behavior tests."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools._bash_environment as bash_environment
from core.storage import TemporaryFileManager
from core.tools.process_manager import ProcessManager
from core.tools.tools import (
    ToolContext,
)

AGENT_ID = "agent-a"

RUN_ID = "run-a"


@pytest.fixture(autouse=True)
def shell_env_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_environment, "_cached_shell_env", {"PATH": "original-path"})
    monkeypatch.setattr(bash_environment, "_shell_env_cache_time", time.monotonic())
    monkeypatch.setattr(bash_environment, "_shell_env_probe_task", None)


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[ProcessManager]:
    manager = ProcessManager(sweep_interval_seconds=3600)
    try:
        yield manager
    finally:
        await manager.aclose()


def make_context(
    tmp_path: Path,
    *,
    cwd: Path | None = None,
    emit_hook: Any = None,
    cancellation_hook: Any = None,
    cancel_registration_hook: Any = None,
    cancel_check_hook: Any = None,
    nesting_depth: int = 0,
    project_id: str | None = None,
    tool_settings: dict[str, object] | None = None,
    skill_env_keys: tuple[str, ...] = (),
) -> ToolContext:
    return ToolContext(
        agent_id=AGENT_ID,
        session_id="session-a",
        run_id=RUN_ID,
        tool_call_id="call-a",
        tool_name="bash",
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        cwd=cwd,
        emit_hook=emit_hook,
        cancellation_hook=cancellation_hook,
        cancel_registration_hook=cancel_registration_hook,
        cancel_check_hook=cancel_check_hook,
        nesting_depth=nesting_depth,
        project_id=project_id,
        tool_settings=tool_settings,
        skill_env_keys=skill_env_keys,
    )


def python_command(command: str) -> list[str]:
    return [sys.executable, "-c", command]


async def kill_background(manager: ProcessManager, result: dict[str, Any]) -> None:
    data = result["data"]
    assert isinstance(data, dict)
    process_id = data["process_id"]
    assert isinstance(process_id, str)
    await manager.kill(process_id, AGENT_ID)


def delivered_future() -> asyncio.Future[None]:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


def make_spool_manager(tmp_path: Path) -> ProcessManager:
    return ProcessManager(
        sweep_interval_seconds=3600,
        temporary_files=TemporaryFileManager(tmp_path),
    )
