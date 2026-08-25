"""Tests for the bash tool's process-manager integration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import types
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools.bash as bash_module
from core.chat import ChatMessage
from core.storage import TemporaryFileManager
from core.tools.bash import (
    BASH_SUBAGENT_TOOL_DESCRIPTION,
    BASH_SUBAGENT_TOOL_PARAMETERS,
    BASH_TOOL_DESCRIPTION,
    BASH_TOOL_PARAMETERS,
    _resolve_background_after_seconds,
    _resolve_workdir,
    background_bash_statuses,
    bash_handler,
    project_bash_tool_definitions,
    register_bash_tool,
)
from core.tools.process import PROCESS_TOOL_NAME, make_process_handler
from core.tools.process_manager import ProcessManager, subprocess_creation_flags
from core.tools.tools import (
    ToolCall,
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolRegistry,
    tool_success,
)

AGENT_ID = "agent-a"
RUN_ID = "run-a"


@pytest.fixture(autouse=True)
def shell_env_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_module, "_cached_shell_env", {"PATH": "original-path"})
    monkeypatch.setattr(bash_module, "_shell_env_cache_time", time.monotonic())
    monkeypatch.setattr(bash_module, "_shell_env_probe_task", None)


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


@pytest.mark.asyncio
async def test_short_command_completes_and_streams_stdout(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit_hook(event_type: str, payload: dict[str, Any]) -> None:
        events.append((event_type, payload))

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, emit_hook=emit_hook)

    result = await bash_handler(
        context,
        {"command": "print('hello')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert result["data"]["output"].replace("\r\n", "\n") == "hello\n"
    assert "stdout" not in result["data"]
    assert "stderr" not in result["data"]
    assert events == [
        (
            "tool_call_stdout",
            {
                "tool_call_id": "call-a",
                "process_id": events[0][1]["process_id"],
                "data": events[0][1]["data"],
            },
        )
    ]
    assert events[0][1]["data"].replace("\r\n", "\n") == "hello\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_source", ["agent", "skill"])
async def test_granted_env_key_is_resolved_into_only_the_spawned_process(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grant_source: str,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(
        tmp_path,
        tool_settings=(
            {"bash": {"allowed_env": ["TEST_API_TOKEN"]}} if grant_source == "agent" else None
        ),
        skill_env_keys=("TEST_API_TOKEN",) if grant_source == "skill" else (),
    )
    resolved: list[str] = []

    def resolve_credential(key: str) -> str:
        resolved.append(key)
        return "hidden-token"

    result = await bash_handler(
        context,
        {
            "command": "import os; print(os.environ['TEST_API_TOKEN'])",
            "mode": "foreground",
            "env_keys": ["TEST_API_TOKEN"],
        },
        manager,
        credential_resolver=resolve_credential,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == "hidden-token"
    assert resolved == ["TEST_API_TOKEN"]
    assert bash_module._cached_shell_env == {"PATH": "original-path"}


@pytest.mark.asyncio
async def test_bash_injects_current_run_context(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, project_id="vbot")

    result = await bash_handler(
        context,
        {
            "command": (
                "import os; print(os.environ['VBOT_RUN_AGENT_ID']); "
                "print(os.environ['VBOT_RUN_SESSION_ID']); "
                "print(os.environ['VBOT_RUN_PROJECT_ID'])"
            ),
            "mode": "foreground",
        },
        manager,
    )

    assert result["data"]["output"].replace("\r\n", "\n") == "agent-a\nsession-a\nvbot\n"


@pytest.mark.asyncio
async def test_identity_bash_removes_host_project_context(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bash_module,
        "_cached_shell_env",
        {"PATH": "original-path", "VBOT_RUN_PROJECT_ID": "host-value"},
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        make_context(tmp_path),
        {
            "command": "import os; print(os.environ.get('VBOT_RUN_PROJECT_ID', 'missing'))",
            "mode": "foreground",
        },
        manager,
    )

    assert result["data"]["output"].strip() == "missing"


@pytest.mark.asyncio
async def test_ungranted_env_key_is_rejected_before_spawn(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    result = await bash_handler(
        make_context(tmp_path),
        {
            "command": "print('must not run')",
            "mode": "foreground",
            "env_keys": ["OPENAI_API_KEY"],
        },
        manager,
        credential_resolver=lambda _key: pytest.fail("credential must not be resolved"),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert "OPENAI_API_KEY" in result["error"]["message"]
    assert manager.list_processes(AGENT_ID) == []


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
    assert result["data"]["mode"] == "background"
    assert result["data"]["delivery"] == "automatic"
    assert isinstance(result["data"]["handoff_note"], str)
    assert result["data"]["handoff_note"]
    assert isinstance(result["data"]["process_id"], str)

    await kill_background(manager, result)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell-specific stdin contract")
async def test_windows_background_process_accepts_raw_stdin_via_process_tool(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    bash_context = make_context(tmp_path)
    bash_result = await bash_handler(
        bash_context,
        {
            "command": ('$value = [Console]::In.ReadLine(); Write-Output "stdin-mode-$value"'),
            "mode": "background",
            "timeout": 10,
        },
        manager,
    )
    bash_data = bash_result["data"]
    assert isinstance(bash_data, dict)
    process_id = bash_data["process_id"]
    assert isinstance(process_id, str)

    process_context = ToolContext(
        agent_id=AGENT_ID,
        session_id=bash_context.session_id,
        run_id=RUN_ID,
        tool_call_id="call-process-input",
        tool_name=PROCESS_TOOL_NAME,
        tool_call_index=1,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
    )
    input_result = await make_process_handler(manager)(
        process_context,
        {
            "action": "input",
            "process_id": process_id,
            "text": "hello-from-input",
            "eof": True,
        },
    )
    tracked = manager.get_process(process_id, AGENT_ID)
    assert tracked.wait_task is not None
    await asyncio.wait_for(tracked.wait_task, timeout=5)
    terminal = await manager.snapshot(process_id, AGENT_ID)

    assert input_result["ok"] is True
    assert terminal["status"] == "completed"
    assert "stdin-mode-hello-from-input" in str(terminal["output"])


@pytest.mark.asyncio
async def test_background_trigger_fires_when_trigger_service_provided(
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


@pytest.mark.asyncio
async def test_background_after_expiry_backgrounds_running_process(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(30)",
            "mode": "auto",
            "background_after_seconds": 0.01,
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert result["data"]["mode"] == "auto"
    assert isinstance(result["data"]["handoff_note"], str)
    assert result["data"]["handoff_note"]

    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_auto_handoff_includes_capped_output_and_usable_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        monkeypatch.setattr(bash_module, "BASH_MODEL_OUTPUT_CAP_CHARS", 50)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {
                "command": (
                    "print('x' * 200 + 'HANDOFF-END', flush=True); import time; time.sleep(30)"
                ),
                "mode": "auto",
                "background_after_seconds": 0.5,
            },
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "running"
        assert data["mode"] == "auto"
        assert data["delivery"] == "automatic"
        assert data["process_note"] == bash_module.BASH_HANDOFF_PROCESS_NOTE
        assert "Use process_id with the process Tool" in data["process_note"]
        assert (
            data["process_note"]
            == "Use process_id with the process Tool for status, raw stdin input, or kill. "
            "Process input writes to a pipe; it does not provide a terminal or TTY. output is "
            "the newest capped snapshot collected before handoff. The result's log_file field "
            "carries the path to the complete combined stdout/stderr stream, written live from "
            "command start through exit."
        )
        process_id = data["process_id"]
        assert isinstance(process_id, str) and process_id
        assert data["truncated"] is True
        assert data["output"].replace("\r\n", "\n").endswith("HANDOFF-END\n")
        assert "[earlier output truncated" in data["output"]
        log_file = Path(data["log_file"])
        assert log_file.exists()
        assert "x" * 200 + "HANDOFF-END" in log_file.read_text(encoding="utf-8")

        process_context = ToolContext(
            agent_id=AGENT_ID,
            session_id=context.session_id,
            run_id=RUN_ID,
            tool_call_id="call-process",
            tool_name=PROCESS_TOOL_NAME,
            tool_call_index=1,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
        )
        process_result = await make_process_handler(spool_manager)(
            process_context,
            {
                "action": "status",
                "process_id": process_id,
            },
        )

        assert process_result["ok"] is True
        assert process_result["data"]["process_id"] == process_id
        assert process_result["data"]["status"] == "running"
    finally:
        tracked_processes = spool_manager.list_processes(AGENT_ID)
        for tracked in tracked_processes:
            if tracked.status == "running":
                await spool_manager.kill(tracked.process_id, AGENT_ID)
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_foreground_mode_never_hands_off(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(
        bash_module,
        "_maybe_spawn_completion_watcher",
        lambda *args, **kwargs: watcher_calls.append((args, kwargs)),
    )
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(0.05); print('finished-inline')",
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["mode"] == "foreground"
    assert "finished-inline" in result["data"]["output"]
    assert watcher_calls == []


@pytest.mark.asyncio
async def test_background_after_is_rejected_outside_auto_mode(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "print('never runs')",
            "mode": "foreground",
            "background_after_seconds": 1,
        },
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize("nesting_depth", [0, 1])
async def test_omitted_execution_mode_defaults_to_foreground(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nesting_depth: int,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=nesting_depth)

    result = await bash_handler(
        context,
        {"command": "print('default-foreground')"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["mode"] == "foreground"
    assert result["data"]["output"].strip() == "default-foreground"


@pytest.mark.asyncio
async def test_invalid_execution_mode_is_rejected_before_spawn(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    result = await bash_handler(
        make_context(tmp_path),
        {"command": "print('never runs')", "mode": "front"},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert manager.list_processes(AGENT_ID) == []


@pytest.mark.asyncio
async def test_explicit_background_at_depth_is_rejected_without_spawning(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent's explicit background request fails before any process spawns."""
    watcher_calls: list[Any] = []
    trigger_calls: list[str] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    class RecordingTriggerService:
        def submit_completion(self, *_args: Any, **_kwargs: Any) -> asyncio.Future[None]:
            trigger_calls.append("called")
            return delivered_future()

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "mode": "background"},
        manager,
        trigger_service=RecordingTriggerService(),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
    assert watcher_calls == []
    await asyncio.sleep(0)
    assert trigger_calls == []


@pytest.mark.asyncio
async def test_automatic_background_at_depth_kills_process_and_fails(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At depth auto mode is killed at background_after_seconds instead of being handed off."""
    watcher_calls: list[Any] = []
    kill_calls: list[tuple[str, str]] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    original_kill = manager.kill

    async def tracking_kill(process_id: str, agent_id: str) -> None:
        kill_calls.append((process_id, agent_id))
        await original_kill(process_id, agent_id)

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(manager, "kill", tracking_kill)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(30)",
            "mode": "auto",
            "background_after_seconds": 0.01,
        },
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
    assert watcher_calls == []
    assert kill_calls, "the still-running process should have been killed"


@pytest.mark.asyncio
async def test_fast_foreground_command_at_depth_succeeds(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent command finishing within background_after_seconds succeeds."""
    watcher_calls: list[Any] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {"command": "print('quick')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "quick" in result["data"]["output"]
    assert watcher_calls == []


@pytest.mark.asyncio
async def test_background_at_top_level_is_not_blocked(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: at depth 0 an explicit background request still backgrounds and watches."""
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
        ) -> asyncio.Future[None]:
            assert session_id
            assert notice_id.startswith("bash:")
            assert origin_run_id == context.run_id
            assert body
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=0)

    result = await bash_handler(
        context,
        {"command": "import sys; sys.exit(0)", "mode": "background"},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    await asyncio.wait_for(trigger_called.wait(), timeout=2)


@pytest.mark.asyncio
async def test_non_zero_exit_code_is_successful_tool_result(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import sys; print('bad', file=sys.stderr); raise SystemExit(7)",
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 7
    assert "bad" in result["data"]["output"]


@pytest.mark.asyncio
async def test_foreground_failure_includes_hint_field(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": (
                "import sys; sys.stderr.write('bash: python: command not found\\n'); sys.exit(127)"
            ),
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 127
    assert result["data"]["hint"] == (
        "This system has no bare `python` — use `python3`, or the project "
        "venv's interpreter (e.g. .venv/bin/python)."
    )


@pytest.mark.asyncio
async def test_foreground_success_omits_hint(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "print('ok')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert "hint" not in result["data"]


@pytest.mark.asyncio
async def test_spawn_failure_returns_failure_envelope(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", lambda command: ["missing-vbot-shell"])
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "ignored", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "process_spawn_failed"


def test_resolve_workdir_defaults_to_cwd_not_workspace(tmp_path: Path) -> None:
    # A project session sets cwd to the repo; with no workdir argument, bash
    # must default its working directory to the cwd, not the agent workspace.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context = make_context(workspace, cwd=repo)

    assert _resolve_workdir(context, None) == repo.resolve()


def test_resolve_workdir_defaults_to_workspace_without_cwd(tmp_path: Path) -> None:
    # No project cwd: the working directory stays the workspace, today's behavior.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = make_context(workspace)

    assert _resolve_workdir(context, None) == workspace.resolve()


def test_resolve_background_after_seconds_uses_generous_default_inside_subagent(
    tmp_path: Path,
) -> None:
    # Top level: an omitted background_after_seconds keeps the short background-hand-off default.
    top = make_context(tmp_path, nesting_depth=0)
    assert (
        _resolve_background_after_seconds(top, None) == bash_module.DEFAULT_BACKGROUND_AFTER_SECONDS
    )
    # Sub-agent: an omitted background_after_seconds gets the generous foreground window instead of
    # the 30s default, so a normal pytest/build is not killed before it finishes.
    sub = make_context(tmp_path, nesting_depth=1)
    assert (
        _resolve_background_after_seconds(sub, None)
        == bash_module.DEFAULT_SUBAGENT_BACKGROUND_AFTER_SECONDS
    )
    assert bash_module.DEFAULT_SUBAGENT_BACKGROUND_AFTER_SECONDS >= 600.0


def test_resolve_background_after_seconds_honors_explicit_value_at_any_depth(
    tmp_path: Path,
) -> None:
    # An explicit background_after_seconds wins at both levels; the caller can still bound tighter.
    assert _resolve_background_after_seconds(make_context(tmp_path, nesting_depth=0), 5.0) == 5.0
    assert _resolve_background_after_seconds(make_context(tmp_path, nesting_depth=1), 5.0) == 5.0


def test_resolve_workdir_resolves_relative_workdir_against_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    context = make_context(workspace, cwd=repo)

    assert _resolve_workdir(context, "sub") == (repo / "sub").resolve()


@pytest.mark.asyncio
async def test_bash_runs_in_cwd_when_no_workdir_argument(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end: the spawned process runs in the cwd, so a relative-path write
    # lands in the repo (cwd), not the agent workspace.
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    context = make_context(workspace, cwd=repo)

    result = await bash_handler(
        context,
        {
            "command": "open('marker.txt', 'w').write('here')",
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert (repo / "marker.txt").read_text(encoding="utf-8") == "here"
    assert not (workspace / "marker.txt").exists()


@pytest.mark.asyncio
async def test_env_argument_is_rejected(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "echo ignored",
            "mode": "foreground",
            "env": {"SAFE_VALUE": "unsupported"},
        },
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_description_is_accepted_without_affecting_command_execution(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    description = "Run the focused Bash verification command with a deliberately long title"

    result = await bash_handler(
        make_context(tmp_path),
        {
            "command": "print('done')",
            "description": description,
            "mode": "foreground",
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == "done"


@pytest.mark.asyncio
async def test_non_string_description_is_rejected(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    result = await bash_handler(
        make_context(tmp_path),
        {"command": "echo ignored", "description": 123, "mode": "foreground"},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_workdir_defaults_to_workspace(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import os; print(os.getcwd())", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == str(tmp_path)


@pytest.mark.asyncio
async def test_timeout_kills_process(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(30)",
            "mode": "auto",
            "timeout": 0.01,
            "background_after_seconds": 1,
        },
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "process_timeout"


@pytest.mark.asyncio
async def test_timeout_remains_active_after_foreground_yields_to_background(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(30)",
            "mode": "auto",
            "timeout": 0.1,
            "background_after_seconds": 0.01,
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    process_id = result["data"]["process_id"]
    poll_result = await manager.poll(process_id, AGENT_ID, timeout_ms=2000)

    assert poll_result["status"] == "killed"


@pytest.mark.asyncio
async def test_natural_completion_at_deadline_not_reported_as_timeout(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that exits on its own as the timer fires reports success.

    Reproduces the deadline race: the timeout flag is already set (the timer
    elapsed) but the process completes naturally, so its kill is a no-op and the
    process ends "completed". The tool must surface that success, not a timeout.
    """
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    def already_timed_out(
        process_manager: ProcessManager,
        process_id: str,
        agent_id: str,
        timeout: float | None,
    ) -> tuple[None, dict[str, bool]]:
        return None, {"timed_out": True}

    monkeypatch.setattr(bash_module, "_schedule_timeout", already_timed_out)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "print('done')",
            "mode": "auto",
            "timeout": 0.01,
            "background_after_seconds": 1,
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert "done" in result["data"]["output"]


@pytest.mark.asyncio
async def test_large_foreground_stdout_is_bounded_and_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProcessManager(buffer_cap_bytes=32, sweep_interval_seconds=3600)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {
                "command": "import sys; sys.stdout.write('a' * 64); sys.stdout.flush()",
                "mode": "foreground",
            },
            manager,
        )

        assert result["ok"] is True
        # No spool dir on this manager: the marker announces the head drop
        # without a log pointer, followed by the surviving newest bytes.
        assert result["data"]["output"] == "[earlier output truncated]\n" + "a" * 32
        assert result["data"]["truncated"] is True
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_run_cancellation_stops_auto_mode_without_handoff(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "FOREGROUND_POLL_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(
        bash_module,
        "_maybe_spawn_completion_watcher",
        lambda *args, **kwargs: watcher_calls.append((args, kwargs)),
    )
    context = make_context(tmp_path, cancellation_hook=lambda: True)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(30)",
            "mode": "auto",
            "background_after_seconds": 30,
        },
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.RUN_CANCELLED_FAILURE_CODE
    assert watcher_calls == []


def test_shell_detection_uses_native_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    assert bash_module._shell_argv("Write-Output hello") == [
        "pwsh",
        "-NonInteractive",
        "-Command",
        "Write-Output hello",
    ]

    monkeypatch.setattr(bash_module.sys, "platform", "linux")

    assert bash_module._shell_argv("echo hello") == ["bash", "-c", "echo hello"]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell-specific regression")
async def test_windows_unknown_pipeline_command_exits_non_interactively(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_cached_shell_env", dict(os.environ))
    context = make_context(tmp_path, nesting_depth=1)

    result = await asyncio.wait_for(
        bash_handler(
            context,
            {
                "command": "Get-ChildItem . | __vbot_missing_pipeline_command__",
                "mode": "foreground",
            },
            manager,
        ),
        timeout=5,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] != 0
    assert "__vbot_missing_pipeline_command__" in result["data"]["output"]


def test_shell_env_probe_requests_windowless_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def creation_flags(*, new_process_group: bool = False) -> int:
        calls.append(new_process_group)
        return 123

    monkeypatch.setattr(bash_module, "subprocess_creation_flags", creation_flags)

    assert bash_module._probe_creationflags() == 123
    assert calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "auto", "background"])
async def test_all_modes_use_managed_windowless_process_group_spawn(
    mode: str,
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, int]] = []

    def creation_flags(
        *,
        new_process_group: bool = False,
        platform_name: str = os.name,
    ) -> int:
        flags = subprocess_creation_flags(
            new_process_group=new_process_group,
            platform_name=platform_name,
        )
        calls.append((new_process_group, flags))
        return flags

    monkeypatch.setattr(
        "core.tools.process_manager.subprocess_creation_flags",
        creation_flags,
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    arguments: dict[str, Any] = {
        "command": ("print('done')" if mode == "foreground" else "import time; time.sleep(30)"),
        "mode": mode,
    }
    if mode == "auto":
        arguments["background_after_seconds"] = 0.01

    result: dict[str, Any] | None = None
    try:
        result = await bash_handler(make_context(tmp_path), arguments, manager)

        expected_flags = subprocess_creation_flags(new_process_group=True)
        assert result["ok"] is True
        assert calls == [(True, expected_flags)]
    finally:
        if result is not None and result["ok"] is True and result["data"]["status"] == "running":
            await kill_background(manager, result)


@pytest.mark.asyncio
async def test_shell_env_probe_timeout_terminates_and_reaps_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed_process_groups: list[tuple[int, int]] = []

    class HungProbe:
        pid = 12345
        returncode = None

        def __init__(self) -> None:
            self.communicate_calls = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.Future()
            self.returncode = -9
            return b"", b""

    probe = HungProbe()

    async def create_probe(*_args: Any, **_kwargs: Any) -> HungProbe:
        return probe

    monkeypatch.setattr(bash_module.sys, "platform", "linux")
    monkeypatch.setattr(bash_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(bash_module, "SHELL_ENV_PROBE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(bash_module.asyncio, "create_subprocess_exec", create_probe)
    monkeypatch.setattr(
        bash_module.os,
        "killpg",
        lambda process_group_id, signal_number: killed_process_groups.append(
            (process_group_id, signal_number)
        ),
        raising=False,
    )
    monkeypatch.setenv("VBOT_PROBE_FALLBACK", "fallback")

    env = await bash_module._probe_shell_env()

    assert env["VBOT_PROBE_FALLBACK"] == "fallback"
    assert killed_process_groups == [(12345, 9)]
    assert probe.communicate_calls == 2


@pytest.mark.asyncio
async def test_concurrent_shell_env_requests_share_one_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        probe_started.set()
        await release_probe.wait()
        return {"PATH": "probed-path"}

    monkeypatch.setattr(bash_module, "_cached_shell_env", None)
    monkeypatch.setattr(bash_module, "_probe_shell_env", probe_shell_env)

    first = asyncio.create_task(bash_module._get_shell_env())
    second = asyncio.create_task(bash_module._get_shell_env())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert probe_calls == 1

    release_probe.set()
    first_env, second_env = await asyncio.gather(first, second)

    assert first_env == {"PATH": "probed-path"}
    assert second_env == {"PATH": "probed-path"}
    assert first_env is not second_env
    assert bash_module._cached_shell_env == {"PATH": "probed-path"}
    assert bash_module._shell_env_probe_task is None


@pytest.mark.asyncio
async def test_cancelling_shell_env_waiter_keeps_shared_probe_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        probe_started.set()
        await release_probe.wait()
        return {"PATH": "probed-path"}

    monkeypatch.setattr(bash_module, "_cached_shell_env", None)
    monkeypatch.setattr(bash_module, "_probe_shell_env", probe_shell_env)

    cancelled_waiter = asyncio.create_task(bash_module._get_shell_env())
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    surviving_waiter = asyncio.create_task(bash_module._get_shell_env())
    await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    assert probe_calls == 1
    assert bash_module._shell_env_probe_task is not None
    assert not bash_module._shell_env_probe_task.cancelled()

    release_probe.set()

    assert await surviving_waiter == {"PATH": "probed-path"}
    assert bash_module._cached_shell_env == {"PATH": "probed-path"}
    assert bash_module._shell_env_probe_task is None


@pytest.mark.asyncio
async def test_shell_env_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale cache triggers a fresh re-probe on the next call."""
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        return {"PATH": f"probe-{probe_calls}"}

    monkeypatch.setattr(bash_module, "_probe_shell_env", probe_shell_env)
    monkeypatch.setattr(bash_module, "SHELL_ENV_CACHE_TTL_SECONDS", 0.01)

    # Seed the cache with a pre-existing value and a fresh timestamp.
    monkeypatch.setattr(bash_module, "_cached_shell_env", {"PATH": "old"})
    monkeypatch.setattr(bash_module, "_shell_env_cache_time", time.monotonic())

    env_first = await bash_module._get_shell_env()
    assert env_first == {"PATH": "old"}
    assert probe_calls == 0  # cache still fresh

    await asyncio.sleep(0.02)  # exceed TTL

    env_second = await bash_module._get_shell_env()
    assert env_second == {"PATH": "probe-1"}
    assert probe_calls == 1  # re-probed after expiry


@pytest.mark.asyncio
async def test_shell_env_cache_ttl_zero_never_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTL of zero means the cache never expires (disables refresh)."""
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        return {"PATH": "fresh"}

    monkeypatch.setattr(bash_module, "_probe_shell_env", probe_shell_env)
    monkeypatch.setattr(bash_module, "SHELL_ENV_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(bash_module, "_cached_shell_env", {"PATH": "cached"})
    monkeypatch.setattr(bash_module, "_shell_env_cache_time", 0.0)

    env = await bash_module._get_shell_env()
    assert env == {"PATH": "cached"}
    assert probe_calls == 0


def test_reset_shell_env_cache_clears_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset_shell_env_cache sets the cache to None so the next call re-probes."""
    monkeypatch.setattr(bash_module, "_cached_shell_env", {"PATH": "stale"})
    monkeypatch.setattr(bash_module, "_shell_env_cache_time", time.monotonic())

    bash_module.reset_shell_env_cache()

    assert bash_module._cached_shell_env is None
    assert bash_module._shell_env_cache_time == 0.0


@pytest.mark.asyncio
async def test_reset_shell_env_cache_forces_reprobe_on_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After reset, the next _get_shell_env call re-probes even if TTL hasn't elapsed."""
    probe_calls = 0

    async def probe_shell_env() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        return {"PATH": f"probe-{probe_calls}"}

    monkeypatch.setattr(bash_module, "_probe_shell_env", probe_shell_env)
    monkeypatch.setattr(bash_module, "SHELL_ENV_CACHE_TTL_SECONDS", 999.0)
    monkeypatch.setattr(bash_module, "_cached_shell_env", None)

    env_first = await bash_module._get_shell_env()
    assert env_first == {"PATH": "probe-1"}
    assert probe_calls == 1

    # Without reset, the cache is fresh (TTL=999) so no re-probe.
    env_cached = await bash_module._get_shell_env()
    assert env_cached == {"PATH": "probe-1"}
    assert probe_calls == 1

    bash_module.reset_shell_env_cache()
    env_after_reset = await bash_module._get_shell_env()
    assert env_after_reset == {"PATH": "probe-2"}
    assert probe_calls == 2


def test_overlay_registry_path_overwrites_path_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_overlay_registry_path replaces PATH with the registry value when available."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")
    monkeypatch.setattr(bash_module, "_read_registry_path", lambda: "C:\\new;C:\\fresh")

    env = {"PATH": "C:\\old", "OTHER": "keep"}
    result = bash_module._overlay_registry_path(env)

    assert result["PATH"] == "C:\\new;C:\\fresh"
    assert result["OTHER"] == "keep"


def test_overlay_registry_path_preserves_path_when_registry_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the registry read returns None, the existing PATH is kept."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")
    monkeypatch.setattr(bash_module, "_read_registry_path", lambda: None)

    env = {"PATH": "C:\\original"}
    result = bash_module._overlay_registry_path(env)

    assert result["PATH"] == "C:\\original"


def test_overlay_registry_path_is_noop_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """On non-Windows, _overlay_registry_path returns the env unchanged."""
    monkeypatch.setattr(bash_module.sys, "platform", "linux")

    env = {"PATH": "/usr/bin:/bin", "HOME": "/home/user"}
    result = bash_module._overlay_registry_path(env)

    assert result == env


def test_read_registry_path_returns_none_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """_read_registry_path returns None on non-Windows without importing winreg."""
    monkeypatch.setattr(bash_module.sys, "platform", "linux")
    assert bash_module._read_registry_path() is None


def test_read_registry_path_combines_machine_and_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows, _read_registry_path joins machine and user PATH segments."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    class FakeKey:
        def __init__(self, path_value: str) -> None:
            self._path_value = path_value

        def __enter__(self) -> FakeKey:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    fake_winreg: Any = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = 1
    fake_winreg.HKEY_CURRENT_USER = 2

    open_key_calls: list[tuple[int, str]] = []

    def fake_open_key(hkey: int, subkey: str) -> FakeKey:
        open_key_calls.append((hkey, subkey))
        if hkey == fake_winreg.HKEY_LOCAL_MACHINE:
            return FakeKey("C:\\system32;C:\\windows")
        return FakeKey("C:\\user\\bin")

    def fake_query_value_ex(key: FakeKey, name: str) -> tuple[str, int]:
        assert name == "PATH"
        return key._path_value, 2  # REG_EXPAND_SZ

    fake_winreg.OpenKey = fake_open_key
    fake_winreg.QueryValueEx = fake_query_value_ex
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    result = bash_module._read_registry_path()
    assert result == "C:\\system32;C:\\windows;C:\\user\\bin"
    assert len(open_key_calls) == 2


def test_read_registry_path_returns_none_when_both_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both machine and user PATH are empty, _read_registry_path returns None."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    class FakeKey:
        def __enter__(self) -> FakeKey:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    fake_winreg: Any = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = 1
    fake_winreg.HKEY_CURRENT_USER = 2

    def fake_open_key(hkey: int, subkey: str) -> FakeKey:
        return FakeKey()

    def fake_query_value_ex(key: FakeKey, name: str) -> tuple[str, int]:
        return "", 2

    fake_winreg.OpenKey = fake_open_key
    fake_winreg.QueryValueEx = fake_query_value_ex
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert bash_module._read_registry_path() is None


def test_read_registry_path_returns_none_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the registry key cannot be opened, _read_registry_path returns None."""
    monkeypatch.setattr(bash_module.sys, "platform", "win32")

    fake_winreg: Any = types.ModuleType("winreg")
    fake_winreg.HKEY_LOCAL_MACHINE = 1
    fake_winreg.HKEY_CURRENT_USER = 2

    def fake_open_key(hkey: int, subkey: str) -> Any:
        raise OSError("registry unavailable")

    fake_winreg.OpenKey = fake_open_key
    fake_winreg.QueryValueEx = lambda *_args: ("", 2)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert bash_module._read_registry_path() is None


@pytest.mark.asyncio
async def test_spawn_filenotfounderror_triggers_env_refresh_and_retry(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FileNotFoundError on spawn resets the env cache and retries once."""
    from core.tools.process_manager import ProcessManager as _ProcMgr

    spawn_calls = 0
    original_spawn = _ProcMgr.spawn

    async def tracking_spawn(self: _ProcMgr, *args: Any, **kwargs: Any) -> str:
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls == 1:
            raise FileNotFoundError("pwsh not found")
        return await original_spawn(self, *args, **kwargs)

    monkeypatch.setattr(_ProcMgr, "spawn", tracking_spawn)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    reset_calls: list[bool] = []
    original_reset = bash_module.reset_shell_env_cache

    def tracking_reset() -> None:
        reset_calls.append(True)
        original_reset()

    monkeypatch.setattr(bash_module, "reset_shell_env_cache", tracking_reset)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "print('recovered')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == "recovered"
    assert spawn_calls == 2
    assert reset_calls == [True]


@pytest.mark.asyncio
async def test_spawn_filenotfounderror_retry_failure_returns_failure_envelope(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the retry also fails with FileNotFoundError, a failure envelope is returned."""
    from core.tools.process_manager import ProcessManager as _ProcMgr

    async def always_fail(self: _ProcMgr, *args: Any, **kwargs: Any) -> str:
        raise FileNotFoundError("still not found")

    monkeypatch.setattr(_ProcMgr, "spawn", always_fail)
    monkeypatch.setattr(bash_module, "reset_shell_env_cache", lambda: None)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "ignored", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "process_spawn_failed"


def test_register_bash_tool() -> None:
    registry = ToolRegistry()
    manager = ProcessManager(sweep_interval_seconds=3600)

    register_bash_tool(registry, manager)

    tool = registry.get("bash")
    assert tool.description == BASH_TOOL_DESCRIPTION
    assert tool.description
    assert tool.parameters == BASH_TOOL_PARAMETERS
    assert "oneOf" not in tool.parameters
    assert "additionalProperties" not in tool.parameters
    assert set(tool.parameters["properties"]) == {
        "mode",
        "command",
        "description",
        "workdir",
        "background_after_seconds",
        "timeout",
        "env_keys",
    }
    assert tool.parameters["required"] == ["command"]
    properties = tool.parameters["properties"]
    assert properties["description"]["type"] == "string"
    assert "maxLength" not in tool.parameters["properties"]["description"]
    assert tool.parameters["properties"]["mode"]["enum"] == [
        "foreground",
        "auto",
        "background",
    ]
    env_keys = properties["env_keys"]
    assert env_keys["type"] == "array"
    assert env_keys["items"] == {"type": "string", "minLength": 1}
    assert env_keys["minItems"] == 1
    assert env_keys["uniqueItems"] is True
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )
    display = registry.display_for_call(
        "bash",
        {
            "description": "Run the frontend tests",
            "command": "npm test -- --run",
            "mode": "foreground",
        },
    )
    assert display["primary"][0]["value"] == "Run the frontend tests"
    assert display["primary"][0]["kind"] == "description"
    assert tool.parameters["properties"]["background_after_seconds"]["default"] == 30
    assert tool.parallel_safe is True


def test_subagent_projection_exposes_only_non_handoff_bash_modes() -> None:
    definitions = [
        {
            "name": "bash",
            "description": BASH_TOOL_DESCRIPTION,
            "parameters": BASH_TOOL_PARAMETERS,
        },
        {
            "name": "read",
            "description": "Read a file.",
            "parameters": {"type": "object"},
        },
    ]

    assert project_bash_tool_definitions(definitions, nesting_depth=0) is definitions

    projected = project_bash_tool_definitions(definitions, nesting_depth=1)
    bash_definition = projected[0]

    assert bash_definition["description"] == BASH_SUBAGENT_TOOL_DESCRIPTION
    assert bash_definition["description"]
    assert bash_definition["parameters"] == BASH_SUBAGENT_TOOL_PARAMETERS
    parameters = bash_definition["parameters"]
    assert "oneOf" not in parameters
    assert "additionalProperties" not in parameters
    assert parameters["required"] == ["command"]
    assert parameters["properties"]["mode"]["enum"] == ["foreground", "auto"]
    assert parameters["properties"]["background_after_seconds"]["default"] == 1800
    assert projected[1] is definitions[1]
    assert definitions[0]["description"] == BASH_TOOL_DESCRIPTION
    assert definitions[0]["parameters"] == BASH_TOOL_PARAMETERS


@pytest.mark.asyncio
async def test_two_bash_calls_can_run_concurrently_by_default(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_count = 0
    max_active_count = 0
    both_started = asyncio.Event()

    async def fake_bash_handler(
        context: ToolContext,
        arguments: dict[str, Any],
        process_manager: ProcessManager,
        *,
        trigger_service: Any | None = None,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> dict[str, Any]:
        nonlocal active_count, max_active_count
        assert process_manager is manager
        assert trigger_service is None
        assert credential_resolver is None
        assert arguments["command"].startswith("download-")
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        if max_active_count == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active_count -= 1
        return tool_success({"status": "completed", "call_id": context.tool_call_id})

    monkeypatch.setattr(bash_module, "bash_handler", fake_bash_handler)
    registry = ToolRegistry()
    register_bash_tool(registry, manager)
    executor = ToolExecutor(registry, per_run_limit=2, global_limit=2)

    results = await executor.execute_many(
        [
            ToolCall(
                id="download-1",
                name="bash",
                arguments={"command": "download-one", "mode": "foreground"},
            ),
            ToolCall(
                id="download-2",
                name="bash",
                arguments={"command": "download-two", "mode": "foreground"},
            ),
        ],
        ToolExecutionConfig(
            agent_id=AGENT_ID,
            session_id="session-a",
            run_id=RUN_ID,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
            allowed_tools=["bash"],
        ),
    )

    assert max_active_count == 2
    assert [result["data"]["call_id"] for result in results] == [
        "download-1",
        "download-2",
    ]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "<1s"),
        (0.4, "<1s"),
        (0.6, "1s"),
        (45.2, "45s"),
        (845.0, "14m 5s"),
        (3723.0, "1h 2m 3s"),
    ],
)
def test_format_elapsed_duration_renders_compact_durations(seconds: float, expected: str) -> None:
    """Elapsed abort times render as compact h/m/s strings."""
    assert bash_module._format_elapsed_duration(seconds) == expected


@pytest.mark.asyncio
async def test_user_cancel_during_foreground_returns_cancelled_by_user_envelope(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-cancel kills the process and returns a ``cancelled_by_user`` envelope."""
    user_cancelled = False
    cancel_calls: list[tuple[str, str]] = []
    kill_event = asyncio.Event()
    registered_callbacks: list[Callable[[], None]] = []

    def cancel_check_hook() -> bool:
        return user_cancelled

    def cancel_registration_hook(callback: Callable[[], None]) -> None:
        registered_callbacks.append(callback)
        # Simulate the runtime marking the call as user-cancelled and
        # firing the cancel callback (which schedules the kill).
        nonlocal user_cancelled
        user_cancelled = True
        callback()

    original_cancel_for_user = manager.cancel_for_user

    async def tracking_cancel_for_user(process_id: str, agent_id: str) -> Any:
        cancel_calls.append((process_id, agent_id))
        try:
            return await original_cancel_for_user(process_id, agent_id)
        finally:
            kill_event.set()

    monkeypatch.setattr(manager, "cancel_for_user", tracking_cancel_for_user)

    context = make_context(
        tmp_path,
        cancel_registration_hook=cancel_registration_hook,
        cancel_check_hook=cancel_check_hook,
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "mode": "foreground"},
        manager,
    )

    await asyncio.wait_for(kill_event.wait(), timeout=2)

    assert result["ok"] is False
    assert result["error"]["code"] == "cancelled_by_user"
    assert result["error"]["message"].startswith("Command aborted by the user after ")
    assert cancel_calls, "process_manager.cancel_for_user should have been called"
    process_id_used, agent_id_used = cancel_calls[0]
    assert agent_id_used == AGENT_ID
    assert isinstance(process_id_used, str) and process_id_used
    assert manager.get_process(process_id_used, AGENT_ID).cancelled_by_user is True
    assert len(registered_callbacks) == 1


@pytest.mark.asyncio
async def test_foreground_completion_unaffected_when_user_cancel_check_is_false(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new check is a no-op when ``was_cancelled_by_user`` returns False."""
    user_cancelled = False
    registered_callbacks: list[Callable[[], None]] = []

    def cancel_check_hook() -> bool:
        return user_cancelled

    def cancel_registration_hook(callback: Callable[[], None]) -> None:
        registered_callbacks.append(callback)

    context = make_context(
        tmp_path,
        cancel_registration_hook=cancel_registration_hook,
        cancel_check_hook=cancel_check_hook,
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        context,
        {"command": "import sys; print('keep-going')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert "keep-going" in result["data"]["output"]
    # The cancel callback was registered but never fired.
    assert len(registered_callbacks) == 1


@pytest.mark.asyncio
async def test_background_watcher_reports_aborted_by_user_when_process_is_user_cancelled(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher uses 'aborted by the user' wording for user-killed processes."""
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
        ) -> asyncio.Future[None]:
            assert session_id
            assert notice_id.startswith("bash:")
            assert origin_run_id == context.run_id
            messages.append(body)
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "import time; time.sleep(30)", "mode": "background"},
        manager,
        trigger_service=MockTriggerService(),
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    data = result["data"]
    assert isinstance(data, dict)
    process_id = data["process_id"]
    assert isinstance(process_id, str) and process_id

    await manager.cancel_for_user(process_id, AGENT_ID)

    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(messages) == 1
    message = messages[0]
    assert "aborted by the user after" in message
    assert "Background process completed." not in message
    assert "Exit code:" not in message
    assert f"Process ID: {process_id}" in message


@pytest.mark.asyncio
async def test_background_watcher_reports_natural_completion_status(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Natural completion identifies the terminal Bash process status."""
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
        ) -> asyncio.Future[None]:
            assert session_id
            assert notice_id.startswith("bash:")
            assert origin_run_id == context.run_id
            messages.append(body)
            trigger_called.set()
            return delivered_future()

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path)

    command = "import sys; print('done'); sys.exit(0)"
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
    message = messages[0]
    assert "### Bash process — completed" in message
    assert "aborted by the user" not in message
    assert "Exit code: 0" in message
    assert "done" in message


def test_background_bash_statuses_folds_handoffs_manual_results_and_completion_notes() -> None:
    messages = [
        ChatMessage.tool(
            tool_call_id="bash-one",
            name="bash",
            content=json.dumps(
                tool_success(
                    {
                        "process_id": "process-one",
                        "status": "running",
                        "delivery": "automatic",
                    }
                )
            ),
        ),
        ChatMessage.tool(
            tool_call_id="foreground",
            name="bash",
            content=json.dumps(tool_success({"process_id": "foreground", "status": "completed"})),
        ),
        ChatMessage.note(
            "Automatic completion delivery\n\n"
            "### Bash process — completed\n"
            "Process ID: process-one\n"
            "Command: npm test"
        ),
        ChatMessage.tool(
            tool_call_id="bash-two",
            name="bash",
            content=json.dumps(
                tool_success(
                    {
                        "process_id": "process-two",
                        "status": "running",
                        "delivery": "automatic",
                    }
                )
            ),
        ),
        ChatMessage.tool(
            tool_call_id="process-two",
            name="process",
            content=json.dumps(tool_success({"process_id": "process-two", "status": "killed"})),
        ),
        ChatMessage.note(
            "### Bash process — aborted by user\nProcess ID: process-three\nCommand: dev server"
        ),
    ]

    assert background_bash_statuses(messages) == {
        "process-one": "completed",
        "process-two": "killed",
        "process-three": "cancelled",
    }


@pytest.mark.asyncio
async def test_user_cancel_kill_failure_is_logged(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing user-cancel kill task is surfaced through the done-callback log.

    The cancel callback schedules ``process_manager.cancel_for_user`` on the running loop and
    attaches ``_log_background_task_result`` as a done-callback. When that kill
    raises, the failure must be logged at error level with a traceback.
    """
    kill_failed = asyncio.Event()

    async def failing_cancel_for_user(process_id: str, agent_id: str) -> None:
        kill_failed.set()
        raise RuntimeError("kill exploded")

    captured_callback: list[Callable[[], None]] = []

    def cancel_registration_hook(callback: Callable[[], None]) -> None:
        captured_callback.append(callback)

    monkeypatch.setattr(manager, "cancel_for_user", failing_cancel_for_user)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    context = make_context(
        tmp_path,
        cancel_registration_hook=cancel_registration_hook,
        cancel_check_hook=lambda: True,
    )
    # Register the user-cancel callback through the handler's wiring without
    # spawning a real process by exercising the registrar directly.
    bash_module._register_user_cancel_callback(manager, context, "process-x")
    assert captured_callback, "cancel callback should have been registered"

    with caplog.at_level(logging.ERROR, logger="vbot.tools.bash"):
        # Fire the cancel callback: it schedules the failing kill task and
        # attaches the logging done-callback.
        captured_callback[0]()
        await asyncio.wait_for(kill_failed.wait(), timeout=2)
        # Let the scheduled kill task finish so its done-callback runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    kill_errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and "user-cancel kill failed" in record.getMessage()
    ]
    assert kill_errors, "expected an error log for the failing user-cancel kill task"
    assert kill_errors[0].exc_info is not None


def make_spool_manager(tmp_path: Path) -> ProcessManager:
    return ProcessManager(
        sweep_interval_seconds=3600,
        temporary_files=TemporaryFileManager(tmp_path),
    )


@pytest.mark.asyncio
async def test_output_cap_keeps_tail_and_names_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        monkeypatch.setattr(bash_module, "BASH_MODEL_OUTPUT_CAP_CHARS", 50)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {
                "command": "print('a' * 200 + 'END-MARKER')",
                "mode": "foreground",
            },
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["truncated"] is True
        assert "END-MARKER" in data["output"]
        assert "[earlier output truncated" in data["output"]
        assert data["output"].index("truncated") < data["output"].index("END-MARKER")

        log_file = Path(data["log_file"])
        content = log_file.read_text(encoding="utf-8")
        assert "a" * 200 + "END-MARKER" in content, "log file must hold the uncut output"
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_small_output_is_not_truncated_and_names_no_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {"command": "print('tiny')", "mode": "foreground"},
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["truncated"] is False
        assert "log_file" not in data
        assert "[earlier output truncated" not in data["output"]
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_background_result_always_names_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {"command": "import time; time.sleep(30)", "mode": "background"},
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "running"
        assert Path(data["log_file"]).exists()

        await kill_background(spool_manager, result)
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_timeout_failure_carries_output_tail_and_log_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {
                "command": ("print('diag-marker', flush=True); import time; time.sleep(30)"),
                "mode": "auto",
                "timeout": 1.5,
                "background_after_seconds": 10,
            },
            spool_manager,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "process_timeout"
        message = result["error"]["message"]
        assert "diag-marker" in message, "output produced before the kill must survive"
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_subagent_kill_failure_carries_output_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path, nesting_depth=1)

        result = await bash_handler(
            context,
            {
                "command": ("print('diag-marker', flush=True); import time; time.sleep(30)"),
                "mode": "auto",
                "background_after_seconds": 1.5,
            },
            spool_manager,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
        message = result["error"]["message"]
        assert "diag-marker" in message
    finally:
        await spool_manager.aclose()


def test_spawn_failure_message_names_missing_shell() -> None:
    message = bash_module._spawn_failure_message(
        ["missing-vbot-shell", "-c", "x"], FileNotFoundError("no such file")
    )

    assert "missing-vbot-shell" in message


def test_spawn_failure_message_explains_pwsh_requirement() -> None:
    message = bash_module._spawn_failure_message(
        ["pwsh", "-Command", "x"], FileNotFoundError("no such file")
    )

    assert "PowerShell 7" in message
