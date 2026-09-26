"""The process Tool's wait action and the call shapes other harnesses use."""

from __future__ import annotations

import asyncio
import re
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools._bash_results as bash_results
import core.tools.process as process_module
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.process import (
    PROCESS_TOOL_NAME,
    normalize_process_arguments,
    register_process_tool,
)
from core.tools.process_manager import ProcessManager
from core.tools.tools import JsonObject, ToolContext, ToolRegistry

AGENT = "agent-a"
RUN = "run-a"
# Prints a start line, a ready line, then keeps running like a server.
SERVER = (
    "import time\n"
    "print('booting', flush=True)\n"
    "time.sleep(0.3)\n"
    "print('Server READY on port 3000', flush=True)\n"
    "time.sleep(30)"
)


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[ProcessManager]:
    manager = ProcessManager(sweep_interval_seconds=3600)
    try:
        yield manager
    finally:
        await manager.aclose()


def make_context(tmp_path: Path, **hooks: Any) -> ToolContext:
    return ToolContext(
        agent_id=AGENT,
        session_id="session-a",
        run_id=RUN,
        tool_call_id="call-a",
        tool_name=PROCESS_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        **hooks,
    )


async def spawn(manager: ProcessManager, script: str, command: str | None = None) -> str:
    return await manager.spawn(
        RUN, AGENT, [sys.executable, "-c", script], env=None, cwd=None, command=command
    )


async def dispatch(manager: ProcessManager, context: ToolContext, arguments: JsonObject):
    registry = ToolRegistry()
    register_process_tool(registry, manager)
    return await registry.dispatch(context, arguments, [PROCESS_TOOL_NAME])


# --- wait ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_returns_the_final_result_when_the_command_exits(manager, tmp_path):
    persisted: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=persisted.append)
    process_id = await spawn(manager, "import time; time.sleep(0.3); print('work done')")

    result = await dispatch(manager, context, {"action": "wait", "process_id": process_id})

    data = result["data"]
    assert (data["status"], data["exit_code"]) == ("completed", 0)
    assert data["output"].strip() == "work done"
    assert "note" not in data
    # Like a terminal status, the result replaces the automatic completion notice.
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_wait_returns_when_a_line_matches_and_the_command_keeps_running(manager, tmp_path):
    process_id = await spawn(manager, SERVER)

    started = time.monotonic()
    result = await dispatch(
        manager,
        make_context(tmp_path),
        {"action": "wait", "process_id": process_id, "pattern": "ready on port \\d+"},
    )

    data = result["data"]
    assert time.monotonic() - started < 10
    assert data["status"] == "running"
    assert data["matched"] == "Server READY on port 3000"
    assert data["output"].splitlines() == ["booting", "Server READY on port 3000"]
    assert data["note"] == "The command is still running; vBot delivers its result when it exits."
    assert manager.get_process(process_id, AGENT).status == "running"


@pytest.mark.asyncio
async def test_output_printed_before_the_wait_still_matches(manager, tmp_path):
    process_id = await spawn(manager, "print('ready', flush=True); import time; time.sleep(30)")
    await manager.wait(process_id, AGENT, timeout_seconds=10, pattern=re.compile("ready"))

    started = time.monotonic()
    result = await dispatch(
        manager,
        make_context(tmp_path),
        {"action": "wait", "process_id": process_id, "pattern": "ready"},
    )

    assert result["data"]["matched"] == "ready"
    assert time.monotonic() - started < 2


@pytest.mark.asyncio
async def test_wait_that_runs_out_of_time_says_what_to_do(manager, tmp_path):
    process_id = await spawn(manager, "import time; time.sleep(30)")

    result = await dispatch(
        manager,
        make_context(tmp_path),
        {"action": "wait", "process_id": process_id, "timeout": 0.3},
    )

    data = result["data"]
    assert data["status"] == "running"
    assert "matched" not in data
    assert data["note"] == (
        "Still running after 0.3 s. If your next step depends on it, wait again; otherwise "
        "continue, and vBot delivers the result when it exits."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "note"),
    [
        (
            {"timeout": 30000},
            "timeout 30000 was read as milliseconds (30 s); timeout takes seconds.",
        ),
        ({"timeout": 5, "timeout_ms": 5000}, None),
        (
            {"pattern": "done (exit"},
            "pattern is not a valid regular expression, so it was matched as plain text.",
        ),
    ],
)
async def test_wait_reads_other_units_and_plain_text_patterns(manager, tmp_path, arguments, note):
    process_id = await spawn(manager, "print('done (exit 0)')")

    result = await dispatch(
        manager, make_context(tmp_path), {"action": "wait", "process_id": process_id, **arguments}
    )

    data = result["data"]
    assert data["output"].strip() == "done (exit 0)"
    expected_note = note
    if "pattern" in arguments:
        # Matching stdout may win the race with process-exit finalization.
        assert data["matched"] == "done (exit 0)"
        assert data["status"] in {"running", "completed"}
        if data["status"] == "running":
            expected_note = (
                f"{note} The command is still running; vBot delivers its result when it exits."
            )
    else:
        assert data["status"] == "completed"
    assert data.get("note") == expected_note


@pytest.mark.asyncio
async def test_wait_longer_than_the_limit_waits_the_limit(manager, tmp_path, monkeypatch):
    monkeypatch.setattr(process_module, "PROCESS_WAIT_MAX_SECONDS", 0.2)
    process_id = await spawn(manager, "import time; time.sleep(30)")

    result = await dispatch(
        manager,
        make_context(tmp_path),
        {"action": "wait", "process_id": process_id, "timeout": 900},
    )

    assert result["data"]["note"].startswith(
        "wait waits at most 0.2 s per call; wait again if the command is still running. "
        "Still running after 0.2 s."
    )


def test_disagreeing_wait_timeouts_fail() -> None:
    with pytest.raises(ValueError, match="process was not run: timeout \\(5 s\\) and timeout_ms"):
        process_module.resolve_timeout(5, 9000, tool_name="process")


@pytest.mark.asyncio
async def test_user_can_end_a_wait_and_keep_the_command(manager, tmp_path):
    controls: list[Callable[[], bool]] = []
    context = make_context(tmp_path, background_registration_hook=controls.append)
    process_id = await spawn(manager, "import time; time.sleep(30)")

    waiting = asyncio.create_task(
        dispatch(manager, context, {"action": "wait", "process_id": process_id, "timeout": 30})
    )
    while not controls:
        await asyncio.sleep(0.01)
    assert controls[0]() is True
    result = await asyncio.wait_for(waiting, 5)

    assert result["data"]["status"] == "running"
    assert result["data"]["note"] == (
        "The user ended this wait. The command is still running, and vBot delivers its "
        "result when it exits."
    )
    assert manager.get_process(process_id, AGENT).status == "running"


@pytest.mark.asyncio
async def test_user_cancelled_wait_leaves_the_command_running(manager, tmp_path):
    cancelled = False
    context = make_context(tmp_path, cancel_check_hook=lambda: cancelled)
    process_id = await spawn(manager, "import time; time.sleep(30)")

    waiting = asyncio.create_task(
        dispatch(manager, context, {"action": "wait", "process_id": process_id, "timeout": 30})
    )
    await asyncio.sleep(0.2)
    cancelled = True
    result = await asyncio.wait_for(waiting, 5)

    assert result["error"] == {
        "code": "cancelled_by_user",
        "message": "The user cancelled this wait. The command is still running.",
        "retryable": False,
    }
    assert manager.get_process(process_id, AGENT).status == "running"


# --- finding the right process ---------------------------------------------


@pytest.mark.asyncio
async def test_wait_without_an_id_lists_the_commands_to_choose_from(manager, tmp_path):
    process_id = await spawn(manager, "import time; time.sleep(30)", command="npm run dev")

    result = await dispatch(manager, make_context(tmp_path), {"action": "wait", "pattern": "ready"})

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "wait needs the process_id of the command to wait for. "
        f"Your commands: {process_id} (running: npm run dev).",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_unknown_id_names_the_agents_commands(manager, tmp_path):
    long_command = "python -m http.server 8000 " + "--flag " * 20
    running = await spawn(manager, "import time; time.sleep(30)", command=long_command)
    finished = await spawn(manager, "print(1)", command="git status")
    await manager.wait(finished, AGENT, timeout_seconds=10)

    result = await dispatch(
        manager, make_context(tmp_path), {"action": "status", "process_id": "proc_missing"}
    )

    label = " ".join(long_command.split())[:57] + "..."
    assert result["error"]["code"] == "process_not_found"
    assert result["error"]["message"] == (
        f"No background command has the id proc_missing. Your commands: {running} "
        f"(running: {label}); {finished} (completed: git status)."
    )


@pytest.mark.asyncio
async def test_terminal_id_points_to_the_terminal_tool(manager, tmp_path):
    result = await dispatch(
        manager, make_context(tmp_path), {"action": "kill", "process_id": "term_abc123"}
    )

    assert result["error"]["message"] == (
        "term_abc123 is a terminal, not a background command. Use the terminal Tool with "
        "this terminal_id."
    )


@pytest.mark.asyncio
async def test_listing_shows_each_command(manager, tmp_path):
    process_id = await spawn(manager, "import time; time.sleep(30)", command="npm run  dev\n")

    result = await dispatch(manager, make_context(tmp_path), {"action": "list"})

    assert [(row["process_id"], row["command"]) for row in result["data"]["processes"]] == [
        (process_id, "npm run dev")
    ]


# --- other harnesses' call shapes ------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # Hermes and Codex name the id session_id; OpenClaw writes sessionId.
        ({"action": "poll", "session_id": "proc_a"}, {"action": "status", "process_id": "proc_a"}),
        (
            {"action": "poll", "sessionId": "proc_a", "timeout": 30000},
            {"action": "wait", "process_id": "proc_a", "timeout": 30000},
        ),
        (
            {"action": "wait", "session_id": "proc_a", "timeout": 30},
            {"action": "wait", "process_id": "proc_a", "timeout": 30},
        ),
        (
            {"action": "log", "process_id": "proc_a", "offset": 0, "limit": 50},
            {"action": "status", "process_id": "proc_a"},
        ),
        ({"action": "list"}, {"action": "status"}),
        ({"action": "stop", "bash_id": "proc_a"}, {"action": "kill", "process_id": "proc_a"}),
        ({"action": "Terminate", "task_id": "proc_a"}, {"action": "kill", "process_id": "proc_a"}),
        (
            {"request": {"operation": "poll", "id": "proc_a"}},
            {"action": "status", "process_id": "proc_a"},
        ),
        # Placeholder values request nothing.
        (
            {"action": "status", "process_id": "proc_a", "filter": "", "limit": 0, "before": " "},
            {"action": "status", "process_id": "proc_a"},
        ),
        ({"action": "status", "process_id": "", "filter": "", "limit": 0}, {"action": "status"}),
        # Fields another action owns are dropped.
        (
            {"action": "kill", "process_id": "proc_a", "filter": "all", "timeout": 10},
            {"action": "kill", "process_id": "proc_a"},
        ),
        (
            {"action": "wait", "process_id": "proc_a", "filter": "running", "limit": 5},
            {"action": "wait", "process_id": "proc_a"},
        ),
        ({"action": "status", "timeout": 5}, {"action": "status"}),
        # A snapshot with a pattern or a timeout is a wait.
        (
            {"action": "status", "process_id": "proc_a", "until": "ready"},
            {"action": "wait", "process_id": "proc_a", "pattern": "ready"},
        ),
        (
            {"action": "status", "process_id": "proc_a", "timeout_ms": 5000},
            {"action": "wait", "process_id": "proc_a", "timeout_ms": 5000},
        ),
    ],
)
def test_other_harness_process_calls_map_onto_the_actions(arguments, expected) -> None:
    assert normalize_process_arguments(arguments) == expected


@pytest.mark.parametrize("action", ["write", "submit", "send_keys", "input"])
def test_input_actions_fail_with_the_alternative(action) -> None:
    with pytest.raises(ValueError) as raised:
        normalize_process_arguments({"action": action, "session_id": "proc_a", "data": "y\n"})
    assert str(raised.value) == (
        "process was not run: background commands take no input; their input is closed when "
        "they start. Run interactive programs with the terminal Tool if you have it, or give "
        "the command its input through a file or a pipeline."
    )


def test_activity_row_shows_the_wait_and_its_pattern() -> None:
    parts = process_module._process_display_parts(
        {"action": "wait", "session_id": "proc_a", "until": "ready"}
    )
    assert [(part.kind, part.value) for part in parts] == [
        ("text", "wait"),
        ("identifier", "proc_a"),
        ("query", "ready"),
    ]


def test_handoff_note_names_the_wait_call() -> None:
    note = bash_results._handoff_note(90)
    assert note.endswith(
        "To wait here until it exits or prints an expected line, call process with action "
        '"wait" and this process_id. Do not start another copy of the command.'
    )
    assert '"wait"' not in bash_results._handoff_note(90, requested_by_user=True)


def test_description_names_the_shell_tool() -> None:
    assert (
        f"Check on, wait for, or stop a `{SHELL_MODEL_NAME}` command that runs in the background."
    ) == process_module.PROCESS_TOOL_DESCRIPTION


@pytest.mark.asyncio
async def test_wait_across_project_scopes_is_not_found(manager, tmp_path):
    process_id = await manager.spawn(
        RUN,
        AGENT,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        project_id="project-a",
        env=None,
        cwd=None,
    )
    context = replace(make_context(tmp_path), project_id="project-b")

    result = await dispatch(manager, context, {"action": "wait", "process_id": process_id})

    assert result["error"]["code"] == "process_not_found"
    assert manager.get_process(process_id, AGENT, project_id="project-a").status == "running"
