"""The Agent-facing process Tool: status, wait, and kill for background shell commands."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

import core.tools.process as process_module
import core.tools.process_manager as manager_module
from core.storage import TemporaryFileManager
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.process import (
    PROCESS_ACTIONS,
    PROCESS_TOOL_DESCRIPTION,
    PROCESS_TOOL_NAME,
    PROCESS_TOOL_PARAMETERS,
    normalize_process_arguments,
    register_process_tool,
)
from core.tools.process_manager import ProcessManager
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure, tool_success
from tests.core.tools.process_manager_test_support import (
    AGENT_A,
    AGENT_B,
    SCOPE_A,
    SLEEP,
    spawn,
)
from tests.core.tools.process_manager_test_support import (
    manager as manager,
)

# Prints a start line, a ready line, then keeps running like a server.
SERVER = (
    "import time\n"
    "print('booting', flush=True)\n"
    "time.sleep(0.3)\n"
    "print('Server READY on port 3000', flush=True)\n"
    "time.sleep(30)"
)


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    return make_context(tmp_path)


def make_context(tmp_path: Path, *, agent_id: str = AGENT_A, **fields: Any) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id="chat-session-a",
        run_id=SCOPE_A,
        tool_call_id="tool-call-a",
        tool_name=PROCESS_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        **fields,
    )


def make_registry(manager: ProcessManager) -> ToolRegistry:
    registry = ToolRegistry()
    register_process_tool(registry, manager)
    return registry


async def dispatch(
    manager: ProcessManager, context: ToolContext, arguments: JsonObject
) -> dict[str, Any]:
    """Call the Tool as the executor does, reporting argument errors as the Model sees them."""
    try:
        return await make_registry(manager).dispatch(context, arguments, [PROCESS_TOOL_NAME])
    except ValueError as error:
        return tool_failure("invalid_arguments", str(error), retryable=False)


# --- contract ----------------------------------------------------------------


def test_schema_exposes_small_flat_action_contract() -> None:
    assert f"`{SHELL_MODEL_NAME}` command" in PROCESS_TOOL_DESCRIPTION
    assert PROCESS_TOOL_PARAMETERS["type"] == "object"
    assert "oneOf" not in PROCESS_TOOL_PARAMETERS
    properties = cast(dict[str, Any], PROCESS_TOOL_PARAMETERS["properties"])
    assert properties["action"]["enum"] == list(PROCESS_ACTIONS)
    assert set(properties) == {
        "action",
        "process_id",
        "timeout",
        "pattern",
        "filter",
        "limit",
        "before",
    }
    assert PROCESS_ACTIONS == ("status", "wait", "kill")
    assert PROCESS_TOOL_PARAMETERS["required"] == ["action"]
    assert "additionalProperties" not in PROCESS_TOOL_PARAMETERS
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"poll": {"process_id": "process-a"}}, '"poll" is not a parameter'),
        ({"action": "clear", "process_id": "process-a"}, '"action" must be one of'),
        ({"action": "status", "text": "value"}, '"text" is not a parameter'),
        ({"action": "status", "filter": "failed"}, '"filter" must be one of'),
        ({"action": "status", "limit": 101}, '"limit" must be at most 100'),
        ({"action": "status", "limit": True}, '"limit" must be an integer'),
        (
            {"action": "wait", "process_id": "proc_a", "timeout": 5, "timeout_ms": 9000},
            "process was not run: timeout (5 s) and timeout_ms (9000 ms) disagree",
        ),
    ],
    ids=[
        "nested-call",
        "unknown-action",
        "input-field",
        "unknown-filter",
        "limit-above-100",
        "limit-not-integer",
        "disagreeing-timeouts",
    ],
)
async def test_invalid_calls_are_rejected(manager, context, arguments, message) -> None:
    result = await dispatch(manager, context, arguments)

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]


@pytest.mark.asyncio
async def test_input_actions_fail_with_the_alternative_and_leave_the_command_alone(
    manager, context
) -> None:
    process_id = await spawn(manager)

    for action in ("write", "submit", "send_keys", "input"):
        result = await dispatch(
            manager,
            context,
            {"action": action, "session_id": process_id, "text": "y\n", "eof": True},
        )
        assert result["error"] == {
            "code": "invalid_arguments",
            "message": (
                "process was not run: background commands take no input; their input is closed "
                "when they start. Run interactive programs with the terminal Tool if you have "
                "it, or give the command its input through a file or a pipeline."
            ),
            "retryable": False,
        }

    tracked = manager.get_process(process_id, AGENT_A)
    assert tracked.status == "running"
    assert tracked.proc is not None and tracked.proc.stdin is None


def test_activity_row_shows_the_normalized_action_and_process_id() -> None:
    registry = make_registry(ProcessManager(sweep_interval_seconds=3600))

    display = registry.display_for_call(
        PROCESS_TOOL_NAME, {"action": "wait", "session_id": "proc_a", "until": "ready"}
    )

    assert [(part["kind"], part["value"]) for part in display["primary"]] == [
        ("text", "wait"),
        ("identifier", "proc_a"),
    ]


# --- status ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_without_process_id_lists_owned_processes_only(manager, context) -> None:
    owned_process_id = await spawn(manager, command="npm run  dev\n")
    await spawn(manager, agent_id=AGENT_B)

    result = await dispatch(manager, context, {"action": "list"})

    assert result["ok"] is True
    assert result["data"]["processes"] == [
        {
            "process_id": owned_process_id,
            "command": "npm run dev",
            "status": "running",
            "exit_code": None,
            "started_at": manager.get_process(owned_process_id, AGENT_A).started_at.isoformat(),
            "finished_at": None,
            "log_file": None,
        }
    ]
    display = make_registry(manager).display_for_call(
        PROCESS_TOOL_NAME, {"action": "status"}, result=result
    )
    assert display["facts"] == [{"kind": "count", "value": 1, "unit": "results", "at_least": False}]
    # The user sees the command and where it stands; ids and paging stay raw.
    assert display["details"] == [
        {
            "type": "results",
            "items": [
                {
                    "title": "npm run dev",
                    "meta": "running",
                    "time": result["data"]["processes"][0]["started_at"],
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_default_hides_130_finished_commands_but_history_remains_retrievable(
    manager, context
):
    process_id = await spawn(manager, "print('retained output')")
    await manager.wait(process_id, AGENT_A, timeout_seconds=10)
    template = manager.get_process(process_id, AGENT_A)
    manager._processes.clear()
    for index in range(130):
        tracked = replace(
            template,
            process_id=f"proc-{index:03d}",
            status=("completed", "failed", "killed")[index % 3],
            started_at=template.started_at + timedelta(seconds=index),
        )
        manager._processes[tracked.process_id] = tracked
    active_id = await spawn(manager)
    callbacks: list[Callable[[], None]] = []
    context = replace(context, result_persisted_hook=callbacks.append)

    default = (await dispatch(manager, context, {"action": "status"}))["data"]
    assert [row["process_id"] for row in default["processes"]] == [active_id]
    assert default["counts"] == {"running": 1, "finished": 130}
    assert default["next_call"] is None
    arguments = default["history_call"]
    seen = []
    while arguments:
        result = await dispatch(manager, context, arguments)
        assert result["ok"] is True
        page = result["data"]
        assert 1 <= len(page["processes"]) <= 20
        seen.extend(row["process_id"] for row in page["processes"])
        arguments = page["next_call"]
    assert seen == [f"proc-{index:03d}" for index in reversed(range(130))]
    assert callbacks == []  # Browsing history must not acknowledge completion.
    detail = await dispatch(manager, context, {"action": "status", "process_id": seen[-1]})
    assert detail["data"]["output"].strip() == "retained output"
    assert len(callbacks) == 1


@pytest.mark.asyncio
async def test_pages_use_stable_boundaries_and_scope_counts(manager, context):
    process_id = await spawn(manager, "print('done')")
    await manager.wait(process_id, AGENT_A, timeout_seconds=10)
    template = manager.get_process(process_id, AGENT_A)
    manager._processes.clear()
    for key in ("a", "b", "c"):
        manager._processes[key] = replace(template, process_id=key)
    manager._processes["foreign-agent"] = replace(
        template, process_id="foreign-agent", agent_id=AGENT_B
    )
    manager._processes["foreign-project"] = replace(
        template, process_id="foreign-project", project_id="other"
    )
    first = (await dispatch(manager, context, {"action": "status", "filter": "all", "limit": 1}))[
        "data"
    ]
    assert first["counts"] == {"running": 0, "finished": 3}
    assert [row["process_id"] for row in first["processes"]] == ["c"]
    manager._processes["new"] = replace(
        template, process_id="new", started_at=template.started_at + timedelta(seconds=1)
    )
    second = (await dispatch(manager, context, first["next_call"]))["data"]
    assert [row["process_id"] for row in second["processes"]] == ["b"]
    del manager._processes["b"]
    expired = await dispatch(manager, context, second["next_call"])
    assert expired["error"]["code"] == "process_not_found"
    for boundary in ("foreign-agent", "foreign-project", "missing"):
        result = await dispatch(manager, context, {"action": "status", "before": boundary})
        assert result["error"]["code"] == "process_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["running", "finished", "all"])
async def test_empty_lists_and_explicit_filters(manager, context, selection):
    result = await dispatch(
        manager, context, {"action": "status", "filter": selection, "limit": 100}
    )
    assert result["data"] == {
        "processes": [],
        "filter": selection,
        "counts": {"running": 0, "finished": 0},
        "next_call": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("with_log", [False, True], ids=["complete", "truncated-with-log"])
async def test_status_with_process_id_returns_a_bounded_non_consuming_snapshot(
    tmp_path: Path, with_log: bool
) -> None:
    manager = ProcessManager(
        sweep_interval_seconds=3600,
        temporary_files=TemporaryFileManager(tmp_path) if with_log else None,
    )
    line_count = 200 if with_log else 1
    try:
        process_id = await spawn(
            manager, f"print('\\n'.join(f'line-{{i}}' for i in range({line_count})))"
        )
        await manager.wait(process_id, AGENT_A, timeout_seconds=10)
        arguments: JsonObject = {"action": "status", "process_id": process_id}

        first = await dispatch(manager, make_context(tmp_path), arguments)
        second = await dispatch(manager, make_context(tmp_path), arguments)
    finally:
        await manager.aclose()

    assert first == second
    data = first["data"]
    assert (data["process_id"], data["status"], data["exit_code"]) == (process_id, "completed", 0)
    assert "stdin_open" not in data
    assert "waiting_for_input" not in data
    if not with_log:
        assert data["output"].strip() == "line-0"
        assert "truncated" not in data
        assert data["log_file"] is None
        return
    assert data["truncated"] is True
    marker, *tail = data["output"].splitlines()
    assert tail == [f"line-{i}" for i in range(100, 200)]
    assert data["log_file"] in marker
    assert Path(data["log_file"]).read_text(encoding="utf-8").splitlines() == [
        f"line-{i}" for i in range(200)
    ]


@pytest.mark.parametrize(
    ("newline", "line_count", "has_log"),
    [("\n", 0, False), ("\n", 100, True), ("\r\n", 101, True), ("\n", 200, False)],
)
def test_output_line_budget_preserves_text_and_log_reference(newline, line_count, has_log):
    lines = [f"line-{index}\n" for index in range(line_count)]
    output = "".join(line.replace("\n", newline) for line in lines)
    log_file = "C:/logs/command.log" if has_log else None
    fields = process_module.shape_process_output(output, log_file=log_file)
    assert fields.get("truncated", False) is (line_count > 100)
    assert len(fields["output"]) <= 8000
    if line_count > 100:
        marker, tail = fields["output"].split("\n", 1)
        assert tail == "".join(lines[-100:])
        if has_log:
            assert fields["log_file"] == log_file
            assert log_file in marker
    else:
        assert fields["output"] == "".join(lines)
        assert "log_file" not in fields


@pytest.mark.parametrize(
    ("output", "shown"),
    [
        ("done\r\nnext\r\n", "done\nnext\n"),
        (
            "Downloading 10%\rDownloading 55%\rDownloading 100%\nsaved\n",
            "Downloading 100%\nsaved\n",
        ),
        ("progress 1/3\rprogress 3/3\r\n", "progress 3/3\n"),
        ("tail without newline\r", "tail without newline"),
        ("plain\n", "plain\n"),
    ],
)
def test_output_shows_carriage_returns_as_a_terminal_leaves_them(output, shown):
    assert process_module.shape_process_output(output)["output"] == shown


@pytest.mark.parametrize(
    ("size", "already_truncated"),
    [(0, False), (8000, False), (8001, False), (7999, True), (20000, True)],
)
def test_output_character_budget_includes_marker(size, already_truncated):
    output = "x" * size
    fields = process_module.shape_process_output(output, truncated=already_truncated)
    assert fields.get("truncated", False) is (already_truncated or size > 8000)
    assert len(fields["output"]) <= 8000
    if fields.get("truncated"):
        assert output.endswith(fields["output"].split("\n", 1)[1])
    else:
        assert fields["output"] == output


# --- kill and completion acknowledgement -------------------------------------


@pytest.mark.asyncio
async def test_kill_stops_the_command_and_a_failed_tree_kill_can_be_retried(
    manager, context, monkeypatch
):
    process_id = await spawn(manager)

    async def denied(proc, **kwargs):
        raise PermissionError("test-owned denied tree")

    with monkeypatch.context() as patch:
        patch.setattr(manager_module, "kill_process_tree_async", denied)
        result = await dispatch(manager, context, {"action": "kill", "process_id": process_id})
        assert result["ok"] is False
        assert result["error"]["code"] == "process_kill_failed"
        assert result["error"]["retryable"] is True
        assert process_id in result["error"]["message"]
        assert manager.get_process(process_id, AGENT_A).status == "running"

    result = await dispatch(manager, context, {"action": "kill", "process_id": process_id})

    assert result == tool_success({"process_id": process_id, "status": "killed"})
    arguments = {"action": "kill", "process_id": process_id}
    assert make_registry(manager).display_for_call(PROCESS_TOOL_NAME, arguments, result=result)[
        "details"
    ] == [{"type": "notice", "level": "info", "text": "The command was stopped."}]


@pytest.mark.parametrize("action", ["status", "kill"])
@pytest.mark.asyncio
async def test_terminal_manual_result_cancels_pending_completion_after_persistence(
    manager: ProcessManager,
    tmp_path: Path,
    action: str,
) -> None:
    callbacks: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    process_id = await spawn(manager, "print('done')" if action == "status" else SLEEP)
    if action == "status":
        await manager.wait(process_id, AGENT_A, timeout_seconds=10)

    notification_release = asyncio.Event()

    async def pending_notification() -> None:
        await notification_release.wait()

    notification_task = asyncio.create_task(pending_notification())
    manager.register_completion_notification(process_id, AGENT_A, notification_task)

    result = await dispatch(manager, context, {"action": action, "process_id": process_id})

    assert result["ok"] is True
    assert len(callbacks) == 1
    assert notification_task.done() is False

    callbacks.pop()()
    await asyncio.sleep(0)

    assert notification_task.cancelled() is True
    assert manager.get_process(process_id, AGENT_A).completion_acknowledged is True


# --- wait --------------------------------------------------------------------


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
async def test_wait_returns_when_a_line_matches_and_the_command_keeps_running(manager, context):
    process_id = await spawn(manager, SERVER)

    result = await asyncio.wait_for(
        dispatch(
            manager,
            context,
            {"action": "wait", "process_id": process_id, "pattern": "ready on port \\d+"},
        ),
        10,
    )

    data = result["data"]
    assert data["status"] == "running"
    assert data["matched"] == "Server READY on port 3000"
    assert data["output"].splitlines() == ["booting", "Server READY on port 3000"]
    assert data["note"] == "The command is still running; vBot delivers its result when it exits."
    assert manager.get_process(process_id, AGENT_A).status == "running"
    arguments = {"action": "wait", "process_id": process_id}
    assert make_registry(manager).display_for_call(PROCESS_TOOL_NAME, arguments, result=result)[
        "details"
    ] == [
        {
            "type": "text",
            "label": "output",
            "source": {"from": "result", "path": ["data", "output"]},
        },
        {
            "type": "notice",
            "level": "info",
            "text": "An output line matched.",
            "subject": "Server READY on port 3000",
        },
        {"type": "notice", "level": "info", "text": "The command is still running."},
    ]


@pytest.mark.asyncio
async def test_output_printed_before_the_wait_still_matches(manager, context):
    process_id = await spawn(manager, "print('ready', flush=True); import time; time.sleep(30)")
    await manager.wait(process_id, AGENT_A, timeout_seconds=10, pattern=re.compile("ready"))

    # A zero timeout returns at once, so only already printed output can match.
    result = await dispatch(
        manager,
        context,
        {"action": "wait", "process_id": process_id, "pattern": "ready", "timeout": 0},
    )

    assert result["data"]["matched"] == "ready"


@pytest.mark.asyncio
async def test_a_wait_that_runs_out_of_time_says_what_to_do(manager, context, monkeypatch):
    monkeypatch.setattr(process_module, "PROCESS_WAIT_MAX_SECONDS", 0)
    process_id = await spawn(manager)

    for arguments, note, ends_turn in [
        ({"timeout": 0}, "Still running after 0 s.", True),
        ({"timeout": 900}, "wait waits at most 0 s per call. Still running after 0 s.", True),
        ({"timeout": 0, "pattern": "ready"}, "No output line matched pattern within 0 s", False),
    ]:
        result = await dispatch(
            manager, context, {"action": "wait", "process_id": process_id, **arguments}
        )

        data = result["data"]
        assert data["status"] == "running"
        assert "matched" not in data
        assert data["note"].startswith(note)
        # Without a pattern the result arrives on its own, so another wait only spends a
        # round trip; a server's result never arrives while it serves.
        assert ("end your turn" in data["note"]) is ends_turn
        assert "wait again" not in data["note"]


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
async def test_wait_reads_other_units_and_plain_text_patterns(manager, context, arguments, note):
    process_id = await spawn(manager, "print('done (exit 0)')")

    result = await dispatch(
        manager, context, {"action": "wait", "process_id": process_id, **arguments}
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
async def test_user_can_end_a_wait_and_keep_the_command(manager, tmp_path):
    controls: list[Callable[[], bool]] = []
    context = make_context(tmp_path, background_registration_hook=controls.append)
    process_id = await spawn(manager)

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
    assert manager.get_process(process_id, AGENT_A).status == "running"


@pytest.mark.asyncio
async def test_user_cancelled_wait_leaves_the_command_running(manager, tmp_path):
    cancelled = False
    waiting_started = asyncio.Event()
    context = make_context(
        tmp_path,
        cancel_check_hook=lambda: cancelled,
        background_registration_hook=lambda _control: waiting_started.set(),
    )
    process_id = await spawn(manager)

    waiting = asyncio.create_task(
        dispatch(manager, context, {"action": "wait", "process_id": process_id, "timeout": 30})
    )
    await asyncio.wait_for(waiting_started.wait(), 5)
    cancelled = True
    result = await asyncio.wait_for(waiting, 5)

    assert result["error"] == {
        "code": "cancelled_by_user",
        "message": "The user cancelled this wait. The command is still running.",
        "retryable": False,
    }
    assert manager.get_process(process_id, AGENT_A).status == "running"


# --- finding the right process -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "project_id", "action"),
    [(AGENT_B, "project-a", "kill"), (AGENT_A, "project-b", "wait"), (AGENT_A, None, "status")],
    ids=["other-agent", "other-project", "no-project"],
)
async def test_another_agent_address_cannot_see_the_process(
    manager, tmp_path, agent_id, project_id, action
):
    process_id = await spawn(manager, project_id="project-a")
    context = make_context(tmp_path, agent_id=agent_id, project_id=project_id)

    result = await dispatch(manager, context, {"action": action, "process_id": process_id})

    assert result == tool_failure(
        "process_not_found",
        f"No background command has the id {process_id}. You have no background commands; a "
        f"`{SHELL_MODEL_NAME}` command that runs in the background returns its process_id.",
        retryable=False,
    )
    assert manager.get_process(process_id, AGENT_A, project_id="project-a").status == "running"


@pytest.mark.asyncio
async def test_wait_without_an_id_lists_the_commands_to_choose_from(manager, context):
    process_id = await spawn(manager, command="npm run dev")

    result = await dispatch(manager, context, {"action": "wait", "pattern": "ready"})

    assert result["error"] == {
        "code": "invalid_arguments",
        "message": "wait needs the process_id of the command to wait for. "
        f"Your commands: {process_id} (running: npm run dev).",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_unknown_id_names_the_agents_commands(manager, context):
    long_command = "python -m http.server 8000 " + "--flag " * 20
    running = await spawn(manager, command=long_command)
    finished = await spawn(manager, "print(1)", command="git status")
    await manager.wait(finished, AGENT_A, timeout_seconds=10)

    result = await dispatch(manager, context, {"action": "status", "process_id": "proc_missing"})

    label = " ".join(long_command.split())[:57] + "..."
    assert result["error"]["code"] == "process_not_found"
    assert result["error"]["message"] == (
        f"No background command has the id proc_missing. Your commands: {running} "
        f"(running: {label}); {finished} (completed: git status)."
    )


@pytest.mark.asyncio
async def test_terminal_id_points_to_the_terminal_tool(manager, context):
    result = await dispatch(manager, context, {"action": "kill", "process_id": "term_abc123"})

    assert result["error"]["message"] == (
        "term_abc123 is a terminal, not a background command. Use the terminal Tool with "
        "this terminal_id."
    )
