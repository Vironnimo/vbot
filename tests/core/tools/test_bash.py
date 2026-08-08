"""Tests for the bash tool's process-manager integration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
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
    _resolve_workdir,
    _resolve_yield_after,
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
    session_id = data["session_id"]
    assert isinstance(session_id, str)
    await manager.kill(session_id, AGENT_ID)


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
                "session_id": events[0][1]["session_id"],
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
    assert manager.list_sessions(AGENT_ID) == []


@pytest.mark.asyncio
async def test_background_mode_returns_running_session_with_clear_handoff(
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
    assert (
        result["data"]["handoff_note"]
        == "The command is still running and has been handed off to vBot immediately. "
        "vBot will monitor it and deliver its terminal result automatically in one "
        "coalesced follow-up Run. You may continue work that does not depend on this "
        "result, or finish the current Run now. Do not poll merely to wait, and do not "
        "start another copy of the command. If your next action depends on the result, "
        "inspect the process explicitly or use foreground mode next time."
    )
    assert isinstance(result["data"]["session_id"], str)

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
    process_session_id = bash_data["session_id"]
    assert isinstance(process_session_id, str)

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
            "session_id": process_session_id,
            "text": "hello-from-input",
            "eof": True,
        },
    )
    process_session = manager.get_session(process_session_id, AGENT_ID)
    assert process_session.wait_task is not None
    await asyncio.wait_for(process_session.wait_task, timeout=5)
    terminal = await manager.snapshot(process_session_id, AGENT_ID)

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
    assert isinstance(result["data"]["session_id"], str)
    await asyncio.sleep(0)
    assert watcher_started.is_set() is False

    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_yield_after_expiry_triggers_background_completion_when_trigger_service_present(
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
            "yield_after": 0.01,
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
    session_id = data["session_id"]
    assert isinstance(session_id, str)

    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    poll_result = await manager.poll(session_id, AGENT_ID, timeout_ms=0)
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
    process_session_id = bash_data["session_id"]
    assert isinstance(process_session_id, str)
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
            "session_id": process_session_id,
        },
    )

    assert process_result["ok"] is True
    assert process_result["data"]["status"] == "completed"
    assert len(persisted_callbacks) == 1

    persisted_callbacks[0]()

    notification_task = manager.get_session(
        process_session_id, AGENT_ID
    ).completion_notification_task
    assert notification_task is not None
    await asyncio.gather(notification_task, return_exceptions=True)
    await asyncio.sleep(0)
    assert notification_task.cancelled() is True
    assert completion_cancelled.is_set() is True


@pytest.mark.asyncio
async def test_yield_after_expiry_backgrounds_running_process(
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
            "yield_after": 0.01,
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    assert result["data"]["mode"] == "auto"
    assert "handed off to vBot after 0.01 seconds" in result["data"]["handoff_note"]

    await kill_background(manager, result)


@pytest.mark.asyncio
async def test_auto_handoff_includes_capped_output_and_usable_process_session(
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
                "yield_after": 0.5,
            },
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "running"
        assert data["mode"] == "auto"
        assert data["delivery"] == "automatic"
        assert data["process_note"] == bash_module.BASH_HANDOFF_PROCESS_NOTE
        assert "Use session_id with the process Tool" in data["process_note"]
        assert (
            data["process_note"]
            == "Use session_id with the process Tool for status, raw stdin input, or kill. "
            "Process input writes to a pipe; it does not provide a terminal or TTY. output is "
            "the newest capped snapshot collected before handoff. The result's log_file field "
            "carries the path to the complete combined stdout/stderr stream, written live from "
            "command start through exit."
        )
        process_session_id = data["session_id"]
        assert isinstance(process_session_id, str) and process_session_id
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
                "session_id": process_session_id,
            },
        )

        assert process_result["ok"] is True
        assert process_result["data"]["session_id"] == process_session_id
        assert process_result["data"]["status"] == "running"
    finally:
        sessions = spool_manager.list_sessions(AGENT_ID)
        for session in sessions:
            if session.status == "running":
                await spool_manager.kill(session.session_id, AGENT_ID)
        await spool_manager.aclose()


def test_auto_handoff_note_uses_agent_facing_contract() -> None:
    assert (
        bash_module._handoff_note("auto", 30)
        == "The command is still running and has been handed off to vBot after 30 seconds. "
        "vBot will monitor it and deliver its terminal result automatically in one "
        "coalesced follow-up Run. You may continue work that does not depend on this "
        "result, or finish the current Run now. Do not poll merely to wait, and do not "
        "start another copy of the command. If your next action depends on the result, "
        "inspect the process explicitly or use foreground mode next time."
    )


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
async def test_yield_after_is_rejected_outside_auto_mode(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {
            "command": "print('never runs')",
            "mode": "foreground",
            "yield_after": 1,
        },
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["message"] == "yield_after is only valid when mode is auto"


@pytest.mark.asyncio
async def test_execution_mode_is_required(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    context = make_context(tmp_path)

    result = await bash_handler(
        context,
        {"command": "print('never runs')"},
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["message"] == "mode must be one of: foreground, auto, background"


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
    """At depth auto mode is killed at yield_after instead of being handed off."""
    watcher_calls: list[Any] = []
    kill_calls: list[tuple[str, str]] = []

    def record_watcher(*args: Any, **kwargs: Any) -> None:
        watcher_calls.append((args, kwargs))

    original_kill = manager.kill

    async def tracking_kill(session_id: str, agent_id: str) -> None:
        kill_calls.append((session_id, agent_id))
        await original_kill(session_id, agent_id)

    monkeypatch.setattr(bash_module, "_maybe_spawn_completion_watcher", record_watcher)
    monkeypatch.setattr(manager, "kill", tracking_kill)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)

    result = await bash_handler(
        context,
        {
            "command": "import time; time.sleep(30)",
            "mode": "auto",
            "yield_after": 0.01,
        },
        manager,
    )

    assert result["ok"] is False
    assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
    assert "Auto mode reached yield_after" in result["error"]["message"]
    assert watcher_calls == []
    assert kill_calls, "the still-running process should have been killed"


@pytest.mark.asyncio
async def test_fast_foreground_command_at_depth_succeeds(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-agent command finishing within yield_after still succeeds synchronously."""
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


def test_resolve_yield_after_uses_generous_default_inside_subagent(tmp_path: Path) -> None:
    # Top level: an omitted yield_after keeps the short background-hand-off default.
    top = make_context(tmp_path, nesting_depth=0)
    assert _resolve_yield_after(top, None) == bash_module.DEFAULT_YIELD_AFTER_SECONDS
    # Sub-agent: an omitted yield_after gets the generous foreground window instead of
    # the 30s default, so a normal pytest/build is not killed before it finishes.
    sub = make_context(tmp_path, nesting_depth=1)
    assert _resolve_yield_after(sub, None) == bash_module.DEFAULT_SUBAGENT_YIELD_AFTER_SECONDS
    assert bash_module.DEFAULT_SUBAGENT_YIELD_AFTER_SECONDS >= 600.0


def test_resolve_yield_after_honors_explicit_value_at_any_depth(tmp_path: Path) -> None:
    # An explicit yield_after wins at both levels, so the caller can still bound tighter.
    assert _resolve_yield_after(make_context(tmp_path, nesting_depth=0), 5.0) == 5.0
    assert _resolve_yield_after(make_context(tmp_path, nesting_depth=1), 5.0) == 5.0


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
    assert result["error"]["message"] == "Unknown argument(s): env"


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

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "description must be a string",
    }


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
            "yield_after": 1,
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
            "yield_after": 0.01,
        },
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "running"
    session_id = result["data"]["session_id"]
    poll_result = await manager.poll(session_id, AGENT_ID, timeout_ms=2000)

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
    session ends "completed". The tool must surface that success, not a timeout.
    """
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    def already_timed_out(
        process_manager: ProcessManager,
        session_id: str,
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
            "yield_after": 1,
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
            "yield_after": 30,
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
        arguments["yield_after"] = 0.01

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


def test_register_bash_tool() -> None:
    registry = ToolRegistry()
    manager = ProcessManager(sweep_interval_seconds=3600)

    register_bash_tool(registry, manager)

    tool = registry.get("bash")
    assert tool.description == BASH_TOOL_DESCRIPTION
    assert tool.parameters == BASH_TOOL_PARAMETERS
    assert "Use foreground when this Run needs the result" in tool.description
    assert "Never manually detach or daemonize a command" in tool.description
    assert "that bypasses vBot's process ownership" in tool.description
    assert "continue independent work or end the Run instead of polling" in tool.description
    assert "oneOf" not in tool.parameters
    assert "additionalProperties" not in tool.parameters
    assert set(tool.parameters["properties"]) == {
        "mode",
        "command",
        "description",
        "workdir",
        "yield_after",
        "timeout",
        "env_keys",
    }
    assert tool.parameters["required"] == ["mode", "command"]
    assert tool.parameters["properties"]["description"] == {
        "type": "string",
        "description": (
            "Short 3–5 word title for the command’s purpose. Omit when the command is "
            "self-explanatory."
        ),
    }
    assert "maxLength" not in tool.parameters["properties"]["description"]
    assert tool.parameters["properties"]["mode"]["enum"] == [
        "foreground",
        "auto",
        "background",
    ]
    assert tool.parameters["properties"]["env_keys"] == {
        "type": "array",
        "description": (
            "Exact names of granted environment credentials to make available to the command. "
            "Omit when no credential is needed."
        ),
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "uniqueItems": True,
    }
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
    assert tool.parameters["properties"]["yield_after"]["default"] == 30
    assert tool.parameters["properties"]["mode"]["description"] == (
        "Execution behavior: foreground waits for completion; auto waits up to yield_after "
        "(default 30s), then hands a still-running command off to vBot; background hands off "
        "immediately — yield_after applies only to auto."
    )
    assert tool.parameters["properties"]["yield_after"]["description"] == (
        'Only valid when mode is "auto". Seconds auto waits before a still-running command is '
        "handed to vBot. Omit for the default (30 seconds); independent of timeout."
    )
    assert "does not extend yield_after" in tool.parameters["properties"]["timeout"]["description"]
    assert (
        "when output is truncated or a command is handed off, the result includes a log_file "
        "path to the complete combined stdout/stderr stream — read or grep it for the full "
        "output." in tool.description
    )
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
    assert bash_definition["parameters"] == BASH_SUBAGENT_TOOL_PARAMETERS
    parameters = bash_definition["parameters"]
    assert "oneOf" not in parameters
    assert "additionalProperties" not in parameters
    assert parameters["properties"]["mode"]["enum"] == ["foreground", "auto"]
    assert parameters["properties"]["yield_after"]["default"] == 1800
    assert parameters["properties"]["mode"]["description"] == (
        "Execution behavior: foreground waits for completion; auto waits until yield_after, "
        "then kills a still-running command because handoff is unavailable — yield_after "
        "applies only to auto."
    )
    assert parameters["properties"]["yield_after"]["description"] == (
        'Only valid when mode is "auto". Seconds auto waits before the command is killed '
        "because process handoff is unavailable. Omit for the default (30 minutes); independent "
        "of timeout."
    )
    assert "process handoff is unavailable" in bash_definition["description"]
    assert (
        "when output is truncated, the result includes a log_file path to the complete combined "
        "stdout/stderr stream." in bash_definition["description"]
    )
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

    async def tracking_cancel_for_user(session_id: str, agent_id: str) -> Any:
        cancel_calls.append((session_id, agent_id))
        try:
            return await original_cancel_for_user(session_id, agent_id)
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
    assert "aborted" in result["error"]["message"].lower()
    assert cancel_calls, "process_manager.cancel_for_user should have been called"
    session_id_used, agent_id_used = cancel_calls[0]
    assert agent_id_used == AGENT_ID
    assert isinstance(session_id_used, str) and session_id_used
    assert manager.get_session(session_id_used, AGENT_ID).cancelled_by_user is True
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
async def test_background_watcher_reports_aborted_by_user_when_session_is_user_cancelled(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher uses 'aborted by the user' wording for user-killed sessions."""
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
    session_id = data["session_id"]
    assert isinstance(session_id, str) and session_id

    await manager.cancel_for_user(session_id, AGENT_ID)

    await asyncio.wait_for(trigger_called.wait(), timeout=2)

    assert len(messages) == 1
    message = messages[0]
    assert "aborted by the user" in message
    assert "Background process completed." not in message
    assert "Exit code:" not in message
    assert f"Process Session: {session_id}" in message


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
                        "session_id": "process-one",
                        "status": "running",
                        "delivery": "automatic",
                    }
                )
            ),
        ),
        ChatMessage.tool(
            tool_call_id="foreground",
            name="bash",
            content=json.dumps(tool_success({"session_id": "foreground", "status": "completed"})),
        ),
        ChatMessage.note(
            "Automatic completion delivery\n\n"
            "### Bash process — completed\n"
            "Process Session: process-one\n"
            "Command: npm test"
        ),
        ChatMessage.tool(
            tool_call_id="bash-two",
            name="bash",
            content=json.dumps(
                tool_success(
                    {
                        "session_id": "process-two",
                        "status": "running",
                        "delivery": "automatic",
                    }
                )
            ),
        ),
        ChatMessage.tool(
            tool_call_id="process-two",
            name="process",
            content=json.dumps(tool_success({"session_id": "process-two", "status": "killed"})),
        ),
        ChatMessage.note(
            "### Bash process — aborted by user\n"
            "Process Session: process-three\n"
            "Command: dev server"
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

    async def failing_cancel_for_user(session_id: str, agent_id: str) -> None:
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
    bash_module._register_user_cancel_callback(manager, context, "session-x")
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
                "yield_after": 10,
            },
            spool_manager,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "process_timeout"
        message = result["error"]["message"]
        assert "diag-marker" in message, "output produced before the kill must survive"
        assert "Complete output:" in message
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
                "yield_after": 1.5,
            },
            spool_manager,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
        message = result["error"]["message"]
        assert "Auto mode reached yield_after" in message
        assert "process handoff is not available inside a Sub-Agent" in message
        assert "diag-marker" in message
    finally:
        await spool_manager.aclose()


def test_spawn_failure_message_names_missing_shell() -> None:
    message = bash_module._spawn_failure_message(
        ["missing-vbot-shell", "-c", "x"], FileNotFoundError("no such file")
    )

    assert "missing-vbot-shell" in message
    assert "was not found" in message


def test_spawn_failure_message_explains_pwsh_requirement() -> None:
    message = bash_module._spawn_failure_message(
        ["pwsh", "-Command", "x"], FileNotFoundError("no such file")
    )

    assert "PowerShell 7" in message
