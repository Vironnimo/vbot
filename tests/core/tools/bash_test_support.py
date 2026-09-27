"""Shared fixtures and fakes for the shell Tool tests."""

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


class RecordingTrigger:
    """A trigger service that records each background completion it receives."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.submitted = asyncio.Event()

    def submit_completion(
        self,
        agent_id: str,
        session_id: str,
        *,
        notice_id: str,
        origin_run_id: str,
        body: str,
        project_id: str | None = None,
        execution_owner: object | None = None,
    ) -> asyncio.Future[None]:
        self.calls.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "notice_id": notice_id,
                "origin_run_id": origin_run_id,
                "body": body,
                "project_id": project_id,
                "execution_owner": execution_owner,
            }
        )
        self.submitted.set()
        return delivered_future()

    async def body(self) -> str:
        """Wait for the one completion and return its message body."""
        await asyncio.wait_for(self.submitted.wait(), 5)
        assert len(self.calls) == 1
        body: str = self.calls[0]["body"]
        return body
