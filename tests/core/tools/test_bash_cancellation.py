"""Bash: cancellation behavior."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import core.tools.bash as bash_module
from core.chat import ChatMessage
from core.tools.bash import (
    background_bash_statuses,
    bash_handler,
)
from core.tools.process_manager import ProcessManager
from core.tools.tools import (
    tool_success,
)
from tests.core.tools.bash_helpers import (
    AGENT_ID,
    delivered_future,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "auto"])
async def test_direct_process_cancel_returns_user_abort_without_run_cancel(
    manager, tmp_path, monkeypatch, mode
):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    ready = asyncio.Event()
    context = replace(
        make_context(tmp_path), background_registration_hook=lambda _callback: ready.set()
    )
    task = asyncio.create_task(
        bash_handler(
            context,
            {
                "command": "import time; time.sleep(30)",
                "mode": mode,
                **({"background_after_seconds": 60} if mode == "auto" else {}),
            },
            manager,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        processes = manager.list_processes(AGENT_ID)
        assert len(processes) == 1
        await manager.cancel_for_user(processes[0].process_id, AGENT_ID)
        result = await asyncio.wait_for(task, timeout=5)
        assert not context.is_cancelled()
        assert not context.was_cancelled_by_user()
        assert result["ok"] is False
        assert result["error"]["code"] == "cancelled_by_user"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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

    async def tracking_cancel_for_user(
        process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> Any:
        cancel_calls.append((process_id, agent_id))
        try:
            return await original_cancel_for_user(process_id, agent_id, project_id=project_id)
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
            execution_owner: object | None = None,
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
            execution_owner: object | None = None,
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

    async def failing_cancel_for_user(
        process_id: str, agent_id: str, *, project_id: str | None = None
    ) -> None:
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
