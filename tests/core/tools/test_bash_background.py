"""Bash: background behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import core.tools.bash as bash_module
from core.tools.bash import (
    bash_handler,
)
from core.tools.process import PROCESS_TOOL_NAME, make_process_handler
from core.tools.process_manager import ProcessManager
from core.tools.tools import (
    ToolContext,
)
from tests.core.tools.bash_helpers import (
    AGENT_ID,
    RUN_ID,
    delivered_future,
    kill_background,
    make_context,
    python_command,
)
from tests.core.tools.bash_helpers import (
    manager as manager,
)
from tests.core.tools.bash_helpers import (
    shell_env_cache as shell_env_cache,
)


@pytest.mark.asyncio
async def test_background_mode_returns_running_process_with_clear_handoff(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "mode": "background"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert "mode" not in result["data"]
    assert result["data"]["delivery"] == "automatic"
    assert isinstance(result["data"]["handoff_note"], str)
    assert result["data"]["handoff_note"]
    assert isinstance(result["data"]["process_id"], str)

    await kill_background(manager, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
async def test_background_trigger_fires_when_trigger_service_provided(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owned: bool,
) -> None:

    from core.runs import RunExecutionOwner

    calls: list[dict[str, Any]] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
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
            calls.append(
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "notice_id": notice_id,
                    "origin_run_id": origin_run_id,
                    "body": body,
                    "execution_owner": execution_owner,
                }
            )
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)
    owner = RunExecutionOwner("swarm", "group", "peer", "generation", "epoch") if owned else None
    context = replace(context, execution_owner=owner)

    result = await bash_handler(
        context,
        {"command": "import sys; sys.exit(0)", "mode": "background"},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(calls) == 1
    assert calls[0]["agent_id"] == AGENT_ID
    assert calls[0]["session_id"] == context.session_id
    assert calls[0]["origin_run_id"] == context.run_id
    assert calls[0]["execution_owner"] == owner


@pytest.mark.asyncio
async def test_background_trigger_not_spawned_when_trigger_service_is_none(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher_started = asyncio.Event()

    async def unexpected_watch(*_args: Any, **_kwargs: Any) -> None:
        watcher_started.set()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "_watch_background_process", unexpected_watch)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "mode": "background"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert isinstance(result["data"]["process_id"], str)
    await asyncio.sleep(0)
    assert watcher_started.is_set() is False

    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_background_after_expiry_triggers_background_completion_when_trigger_service_present(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
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
            calls.append(
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "notice_id": notice_id,
                    "origin_run_id": origin_run_id,
                    "body": body,
                }
            )
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; print('yield-marker'); time.sleep(0.2)",
            "mode": "auto",
            "background_after_seconds": 0.01,
        },
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(calls) == 1
    assert calls[0]["agent_id"] == AGENT_ID
    assert calls[0]["session_id"] == context.session_id
    assert calls[0]["origin_run_id"] == context.run_id
    assert "yield-marker" in calls[0]["body"]


@pytest.mark.asyncio
async def test_background_trigger_message_contains_command_exit_code_and_output(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
        def submit_completion(
            self,
            _agent_id: str,
            session_id: str,
            *,
            notice_id: str,
            origin_run_id: str,
            body: str,
            project_id: str | None = None,
            execution_owner: object | None = None,
        ) -> asyncio.Future[None]:
            messages.append(body)
            assert session_id
            assert notice_id.startswith("bash:")
            assert origin_run_id == context.run_id
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)
    command = "import sys; print('result-marker'); sys.exit(3)"

    result = await bash_handler(
        context,
        {"command": command, "mode": "background"},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(messages) == 1
    assert f"Command: {command}" in messages[0]
    assert "Exit code: 3" in messages[0]
    assert "result-marker" in messages[0]


@pytest.mark.asyncio
async def test_background_trigger_message_carries_failure_hint(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed background command's automatic note includes the output hint."""
    messages: list[str] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
        def submit_completion(
            self,
            _agent_id: str,
            session_id: str,
            *,
            notice_id: str,
            origin_run_id: str,
            body: str,
            project_id: str | None = None,
            execution_owner: object | None = None,
        ) -> asyncio.Future[None]:
            messages.append(body)
            assert session_id
            assert origin_run_id == context.run_id
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)
    command = "import sys; sys.stderr.write('bash: python: command not found\\n'); sys.exit(127)"

    result = await bash_handler(
        context,
        {"command": command, "mode": "background"},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(messages) == 1
    assert "Exit code: 127" in messages[0]
    assert "Hint: " in messages[0]
    assert "python3" in messages[0]


@pytest.mark.asyncio
async def test_background_completion_trigger_carries_project_id(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project-scoped background completion wakes the parent run under its project."""
    captured: list[str | None] = []
    trigger_called = asyncio.Event()

    class MockTriggerService:
        def submit_completion(
            self,
            _agent_id: str,
            session_id: str,
            *,
            notice_id: str,
            origin_run_id: str,
            body: str,
            project_id: str | None = None,
            execution_owner: object | None = None,
        ) -> asyncio.Future[None]:
            assert session_id
            assert notice_id.startswith("bash:")
            assert origin_run_id == context.run_id
            assert body
            captured.append(project_id)
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, project_id="acme")

    result = await bash_handler(
        context,
        {"command": "import sys; sys.exit(0)", "mode": "background"},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)
    assert captured == ["acme"]


@pytest.mark.asyncio
async def test_background_watcher_does_not_consume_process_poll_output(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger_called = asyncio.Event()

    class MockTriggerService:
        def submit_completion(
            self,
            _agent_id: str,
            session_id: str,
            *,
            notice_id: str,
            origin_run_id: str,
            body: str,
            project_id: str | None = None,
            execution_owner: object | None = None,
        ) -> asyncio.Future[None]:
            assert session_id
            assert notice_id.startswith("bash:")
            assert origin_run_id == context.run_id
            assert body
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; print('poll-marker'); time.sleep(0.05)",
            "mode": "background",
        },
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    data = result["data"]
    assert isinstance(data, dict)
    process_id = data["process_id"]
    assert isinstance(process_id, str)

    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    poll_result = await manager.poll(process_id, AGENT_ID, timeout_ms=0)
    output = poll_result.get("output")
    assert isinstance(output, str)
    assert "poll-marker" in output


@pytest.mark.asyncio
async def test_terminal_process_status_cancels_already_pending_completion_delivery(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion_submitted = asyncio.Event()
    completion_cancelled = asyncio.Event()

    class PendingTriggerService:
        def __init__(self) -> None:
            self.delivery: asyncio.Future[None] | None = None

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
            assert agent_id == AGENT_ID
            assert session_id == "session-a"
            assert project_id is None
            assert notice_id.startswith("bash:")
            assert origin_run_id == RUN_ID
            assert body
            self.delivery = asyncio.get_running_loop().create_future()
            completion_submitted.set()
            return self.delivery

        def cancel_completion(
            self,
            agent_id: str,
            session_id: str,
            *,
            notice_id: str,
            project_id: str | None = None,
        ) -> bool:
            assert agent_id == AGENT_ID
            assert session_id == "session-a"
            assert notice_id.startswith("bash:")
            assert project_id is None
            if self.delivery is not None and not self.delivery.done():
                self.delivery.cancel()
            completion_cancelled.set()
            return True

    trigger_service = PendingTriggerService()
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    bash_context = make_context(tmp_path)
    bash_result = await bash_handler(
        bash_context,
        {"command": "print('done')", "mode": "background"},
        manager,
        trigger_service=trigger_service,
    )
    bash_data = bash_result["data"]
    assert isinstance(bash_data, dict)
    process_id = bash_data["process_id"]
    assert isinstance(process_id, str)
    await asyncio.wait_for(completion_submitted.wait(), timeout=2)

    persisted_callbacks: list[Callable[[], None]] = []
    process_context = ToolContext(
        agent_id=AGENT_ID,
        session_id=bash_context.session_id,
        run_id=RUN_ID,
        tool_call_id="call-process",
        tool_name=PROCESS_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        result_persisted_hook=lambda callback: persisted_callbacks.append(callback),
    )
    process_result = await make_process_handler(manager)(
        process_context,
        {
            "action": "status",
            "process_id": process_id,
        },
    )

    assert process_result["ok"] is True
    assert process_result["data"]["status"] == "completed"
    assert len(persisted_callbacks) == 1

    persisted_callbacks[0]()

    notification_task = manager.get_process(process_id, AGENT_ID).completion_notification_task
    assert notification_task is not None
    await asyncio.gather(notification_task, return_exceptions=True)
    await asyncio.sleep(0)
    assert notification_task.cancelled() is True
    assert completion_cancelled.is_set() is True
