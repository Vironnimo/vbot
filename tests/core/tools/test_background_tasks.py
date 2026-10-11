"""background_tasks: list, check and stop background commands; never input, never a wait.

Commands run on fake processes through a real ``CommandManager`` on a fake clock
(``command_manager_helpers``); a fake Sub-Agent tree says which Sessions descend
from the caller. Calls go through the registry as the executor sends them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.sessions import SessionAddress
from core.tools.availability import ToolAccess, resolve_tool_access
from core.tools.background_tasks import (
    BACKGROUND_TASKS_TOOL_NAME,
    register_background_tasks_tool,
)
from core.tools.command_manager import CommandManager
from core.tools.model_names import called_tool_name
from core.tools.process_tree import RunningProcess
from core.tools.shell import SHELL_TOOL_NAME, register_shell_tool
from core.tools.terminal_manager import TerminalManager
from core.tools.tools import JsonObject, ToolContext, ToolRegistry
from tests.core.tools.command_manager_helpers import (
    OWNER,
    FakeProcess,
    Harness,
    command_harness,
    format_report,
)
from tests.core.tools.terminal_helpers import call as terminal_call
from tests.core.tools.terminal_helpers import make_context as terminal_context
from tests.core.tools.terminal_helpers import manager as manager
from tests.core.tools.terminal_manager_helpers import AdapterFactory
from tests.core.tools.tools_test_support import dispatch_as_executor

# The caller's Sub-Agent, and a Session outside the caller's tree.
CHILD = SessionAddress("project-a", "sub-agent", "session-child")
SIBLING = SessionAddress("project-a", "sub-agent-2", "session-sibling")
SERVER = RunningProcess(5001, "server.exe")


class FakeSubAgents:
    """The Sub-Agent tree: the caller's Sub-Agent ``sub_child`` runs in ``CHILD``."""

    async def descendant_sessions(self, caller: SessionAddress) -> Mapping[SessionAddress, str]:
        return {CHILD: "sub_child"} if caller == OWNER else {}


class Tasks:
    """``background_tasks`` and the shell over one ``CommandManager`` with fakes."""

    def __init__(self, commands: Harness) -> None:
        self.commands = commands
        self.registry = ToolRegistry()
        register_shell_tool(self.registry, commands.manager)
        register_background_tasks_tool(self.registry, commands.manager, subagents=FakeSubAgents())
        # Callbacks the calls registered to run once their Tool Results are persisted.
        self.persisted: list[Callable[[], None]] = []

    def context(self, owner: SessionAddress = OWNER) -> ToolContext:
        tmp_path = self.commands.cwd
        return ToolContext(
            agent_id=owner.agent_id,
            session_id=owner.session_id,
            run_id="run-b",
            tool_call_id="call-b",
            tool_name=BACKGROUND_TASKS_TOOL_NAME,
            tool_call_index=0,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
            cwd=tmp_path,
            project_id=owner.project_id,
            result_persisted_hook=self.persisted.append,
        )

    async def call(
        self,
        arguments: Any,
        *,
        owner: SessionAddress = OWNER,
        tools: tuple[str, ...] = (SHELL_TOOL_NAME, "terminal"),
    ) -> JsonObject:
        """One call as the executor sends it; a call that waits on time fails the test."""
        allowed = [BACKGROUND_TASKS_TOOL_NAME, *tools]
        return await asyncio.wait_for(
            dispatch_as_executor(self.registry, self.context(owner), arguments, allowed), 5
        )

    async def background(
        self, *, owner: SessionAddress = OWNER, command: str = "serve"
    ) -> tuple[str, FakeProcess]:
        """A command of *owner* that continues in the background and delivers its result."""
        command_id, process = await self.commands.launch(
            owner=owner, command=command, formatter=format_report
        )
        self.commands.manager.to_background(command_id, deliver=True)
        # Each command starts a second after the one before.
        await self.commands.advance(1)
        return command_id, process

    def persist(self) -> None:
        for callback in self.persisted:
            callback()


@pytest_asyncio.fixture
async def tasks(tmp_path: Path) -> AsyncIterator[Tasks]:
    async with command_harness(tmp_path, max_live=8) as commands:
        yield Tasks(commands)


def data(result: JsonObject) -> JsonObject:
    assert result["ok"] is True, result
    payload: JsonObject = result["data"]
    return payload


def error(result: JsonObject) -> JsonObject:
    assert result["ok"] is False, result
    failure: JsonObject = result["error"]
    return failure


_CALL = re.compile(r"background_tasks (\{.*?\})")


def calls(text: str) -> list[JsonObject]:
    """The complete ``background_tasks`` calls a text names."""
    return [json.loads(call) for call in _CALL.findall(text)]


_OUTPUT_FILE = re.compile(r"(?:written live to|The complete output is in) (\S+)\.(?=\s|$)")


def output_file(text: str) -> Path:
    """The existing file with a command's complete output that *text* names."""
    match = _OUTPUT_FILE.search(text)
    assert match is not None, text
    path = Path(match[1])
    assert path.is_file()
    return path


# --- list --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shows_the_background_commands_of_this_session_and_its_subagents(
    tasks: Tasks,
) -> None:
    running, _ = await tasks.background(command="serve")
    ended, ended_process = await tasks.background(command="build")
    ended_process.exit(1)
    await tasks.commands.finish(ended)
    child, _ = await tasks.background(owner=CHILD, command="watch")
    sibling, _ = await tasks.background(owner=SIBLING, command="other")
    # A command still inside its call is no background task.
    await tasks.commands.launch(command="foreground")
    await tasks.commands.advance(60, step=5)

    listed = data(await tasks.call({"action": "list"}))

    rows = listed["tasks"]
    assert [row["id"] for row in rows] == [child, ended, running]
    for row in rows:
        assert row["type"] == "command"
        assert Path(row["log"]).is_file()
    assert [(row["command"], row["status"], row.get("subagent")) for row in rows] == [
        ("watch", "running", "sub_child"),
        ("build", "exited", None),
        ("serve", "running", None),
    ]
    assert rows[1]["exit_code"] == 1
    assert rows[2]["runtime"].startswith("1 minute ")
    # The caller's own command runs: the note says its result arrives by itself.
    assert len(listed["notes"]) == 1
    # The user sees each listed command.
    shown = tasks.registry.display_for_call(
        BACKGROUND_TASKS_TOOL_NAME, {"action": "list"}, result={"ok": True, "data": listed}
    )["details"]
    assert [item["title"] for item in shown[0]["items"]] == ["watch", "build", "serve"]

    # A Sub-Agent sees its own commands, never its parent's or a sibling's.
    own = data(await tasks.call({"action": "list"}, owner=CHILD))["tasks"]
    assert [(row["id"], "subagent" in row) for row in own] == [(child, False)]
    assert sibling not in json.dumps(listed)

    empty = data(await tasks.call({"action": "list"}, owner=SessionAddress("p", "a", "s")))
    assert (empty["tasks"], len(empty["notes"])) == ([], 1)


# --- status ------------------------------------------------------------------


@pytest.mark.parametrize("owner", [OWNER, CHILD], ids=["own", "subagent"])
@pytest.mark.asyncio
async def test_status_shows_a_running_command_with_its_newest_output(
    tasks: Tasks, owner: SessionAddress
) -> None:
    command_id, process = await tasks.background(owner=owner)
    process.write("listening on 8000\n")
    await tasks.commands.advance(30, step=5)

    status = data(await tasks.call({"action": "status", "id": command_id}))

    assert (status["status"], status["command_id"], status["output"]) == (
        "running",
        command_id,
        "listening on 8000",
    )
    own = owner == OWNER
    assert status.get("subagent") == (None if own else "sub_child")
    # Its result goes to the Session that ran it; the Agent can stop it with the call named.
    assert ("sub_child" in status["next"]) is not own
    assert calls(status["next"]) == [{"action": "stop", "id": command_id}]
    output_file(status["next"])
    # Looking changes nothing.
    report = tasks.commands.manager.report(command_id)
    assert (report.ended, report.delivers_result, process.tree.terminations) == (False, True, 0)


@pytest.mark.parametrize("owner", [OWNER, CHILD], ids=["own", "subagent"])
@pytest.mark.asyncio
async def test_status_of_an_ended_command_acknowledges_only_this_sessions_delivery(
    tasks: Tasks, owner: SessionAddress
) -> None:
    command_id, process = await tasks.background(owner=owner)
    process.write("done\n")
    process.exit(0)
    await tasks.commands.finish(command_id)
    # Submitted, not yet handed over to the Session.
    assert len(tasks.commands.sink.submissions) == 1

    status = data(await tasks.call({"action": "status", "id": command_id}))
    tasks.persist()

    assert (status["status"], status["exit_code"], status["output"]) == ("exited", 0, "done")
    output_file(status["next"])
    # The result showed the end to the Agent that waits for it; a Sub-Agent's stays.
    own = owner == OWNER
    assert tasks.commands.sink.cancellations == ([f"command:{command_id}"] if own else [])


# --- stop --------------------------------------------------------------------


@pytest.mark.parametrize("owner", [OWNER, CHILD], ids=["own", "subagent"])
@pytest.mark.asyncio
async def test_stop_stops_a_command_and_tells_its_session_when_an_ancestor_stopped_it(
    tasks: Tasks, owner: SessionAddress
) -> None:
    command_id, process = await tasks.background(owner=owner)
    process.write("serving\n")

    stopped = data(await tasks.call({"action": "stop", "id": command_id}))
    report = await tasks.commands.finish(command_id)

    assert (stopped["status"], stopped["stopped_because"], stopped["output"]) == (
        "stopped",
        "you stopped it.",
        "serving",
    )
    output_file(stopped["next"])
    assert process.tree.terminations == 1
    if owner == OWNER:
        # The stop result is the outcome: nothing is delivered.
        assert (report.stop_reason, tasks.commands.sink.submissions) == ("agent", [])
        assert "notes" not in stopped
    else:
        # The Sub-Agent did not see the stop: its Session gets the result.
        (submission,) = tasks.commands.sink.submissions
        assert (report.stop_reason, submission["session_id"]) == ("parent", CHILD.session_id)
        assert "sub_child" in stopped["notes"][0]


@pytest.mark.parametrize("leftovers", [False, True], ids=["ended", "left-processes-running"])
@pytest.mark.asyncio
async def test_stopping_an_ended_command_succeeds_and_stops_what_it_left_running(
    tasks: Tasks, leftovers: bool
) -> None:
    command_id, process = await tasks.background()
    process.exit(0, leftovers=[SERVER] if leftovers else [])
    await tasks.commands.advance(4)
    assert tasks.commands.manager.report(command_id).ended

    stopped = data(await tasks.call({"action": "stop", "id": command_id}))
    tasks.persist()

    # The command keeps its outcome.
    assert (stopped["status"], stopped["exit_code"]) == ("exited", 0)
    (note,) = stopped["notes"]
    assert ("server.exe (pid 5001)" in note) is leftovers
    report = tasks.commands.manager.report(command_id)
    assert (report.finished, report.still_running) == (True, ())
    assert process.tree.terminations == (1 if leftovers else 0)
    # The result showed the end: a delivery not yet handed over is withdrawn.
    assert tasks.commands.sink.cancellations == [f"command:{command_id}"]


@pytest.mark.asyncio
async def test_an_unconfirmed_stop_says_what_may_still_run_and_how_to_retry(
    tasks: Tasks,
) -> None:
    command_id, process = await tasks.background()
    process.tree.terminate_error = OSError("access denied")

    failure = error(await tasks.call({"action": "stop", "id": command_id}))
    process.tree.terminate_error = None

    assert (failure["code"], failure["retryable"]) == ("stop_failed", True)
    assert f"shell (pid {process.pid})" in failure["message"]
    # The call it names stops the command once the operating system lets it.
    (retry,) = calls(failure["message"])
    assert retry == {"action": "stop", "id": command_id}
    assert data(await tasks.call(retry))["status"] == "stopped"


# --- Ids and argument shapes ---------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "outcome"),
    [
        ({"action": "kill", "task_id": "{id}"}, "stopped"),
        ({"action": "output", "bash_id": "{id}"}, "running"),
        ({"action": "status", "terminal_id": "{id}"}, "running"),
        ({"action": "status", "id": ["{id}"]}, "running"),
        ({"action": "ls", "id": "{id}"}, "listed"),
        # Without an action an id is only looked at, also under a harness's name.
        ({"shell_id": "{id}"}, "looked-at"),
    ],
)
@pytest.mark.asyncio
async def test_other_harnesses_shapes_reach_the_intended_action(
    tasks: Tasks, arguments: JsonObject, outcome: str
) -> None:
    command_id, process = await tasks.background()
    sent = json.loads(json.dumps(arguments).replace("{id}", command_id))
    # Claude Code's KillShell is this Tool.
    name = called_tool_name("KillShell", [SHELL_TOOL_NAME, BACKGROUND_TASKS_TOOL_NAME])
    assert name == BACKGROUND_TASKS_TOOL_NAME

    result = data(await tasks.call(sent))

    if outcome == "listed":
        assert [row["id"] for row in result["tasks"]] == [command_id]
        return
    assert result["status"] == ("stopped" if outcome == "stopped" else "running")
    assert process.tree.terminations == (1 if outcome == "stopped" else 0)
    if outcome == "looked-at":
        # The note names the stop the Agent may have meant.
        assert calls(result["notes"][0]) == [{"action": "stop", "id": command_id}]
    else:
        assert "notes" not in result


@pytest.mark.asyncio
async def test_ids_outside_this_session_and_its_subagents_are_refused(tasks: Tasks) -> None:
    own, _ = await tasks.background(command="serve")
    sibling, sibling_process = await tasks.background(owner=SIBLING)
    foreground, _ = await tasks.commands.launch(command="foreground")
    parent_view = tasks.commands.manager.report(own).command_id

    refusals = [
        await tasks.call({"action": "stop", "id": sibling}),
        await tasks.call({"action": "status", "id": foreground}),
        await tasks.call({"action": "status", "id": "sub_unknown"}),
        # A Sub-Agent never reaches its parent's commands.
        await tasks.call({"action": "stop", "id": parent_view}, owner=CHILD),
    ]
    missing = error(await tasks.call({"action": "stop"}))
    several = error(await tasks.call({"action": "stop", "id": [own, sibling]}))

    for refusal in refusals[:3]:
        failure = error(refusal)
        assert failure["code"] == "task_not_found"
        # The refusal names what of the caller's runs now.
        assert f"{own} (serve)" in failure["message"]
    assert error(refusals[3])["code"] == "task_not_found"
    assert missing["code"] == "invalid_arguments" and f"{own} (serve)" in missing["message"]
    assert several["code"] == "invalid_arguments"
    assert sibling_process.tree.terminations == 0
    assert not tasks.commands.manager.report(own).ended


# --- Availability ----------------------------------------------------------------


def test_background_tasks_is_available_exactly_when_the_shell_is() -> None:
    commands = CommandManager()
    tools = ToolRegistry()
    register_shell_tool(tools, commands)
    register_background_tasks_tool(tools, commands)
    catalog = tools.list_tools()

    def allowed(policy: ToolAccess) -> set[str]:
        return set(resolve_tool_access(policy, catalog, "off").allowed_tools)

    both = {SHELL_TOOL_NAME, BACKGROUND_TASKS_TOOL_NAME}
    assert allowed(ToolAccess(mode="all")) == both
    assert allowed(ToolAccess(mode="selected", allowed=(SHELL_TOOL_NAME,))) == both
    assert allowed(ToolAccess(mode="all", denied=(SHELL_TOOL_NAME,))) == set()
    assert allowed(ToolAccess(mode="selected", allowed=(BACKGROUND_TASKS_TOOL_NAME,))) == set()
    # The user configures the shell; its companion is no switch of its own.
    assert [tool.name for tool in tools.list_tools(include_catalog_hidden=False)] == [
        SHELL_TOOL_NAME
    ]


# --- User requirement --------------------------------------------------------------


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.asyncio
async def test_background_tasks_sends_no_input_and_never_waits(
    tasks: Tasks, terminal: bool
) -> None:
    """User requirement: background_tasks never gives a command input and never waits."""
    command_id, process = await tasks.background()
    process.write("Continue? [y/N] ")
    await tasks.commands.advance(1)
    before = tasks.commands.manager.latest_output(command_id, 5)
    started_at = tasks.commands.clock.now
    tools = (SHELL_TOOL_NAME, "terminal") if terminal else (SHELL_TOOL_NAME,)
    inputs = [
        {"action": "input", "id": command_id, "text": "y\n"},
        {"action": "send_keys", "id": command_id, "keys": ["Enter"]},
        {"action": "write", "id": command_id, "content": "y"},
        {"action": "stdin", "id": command_id, "input": "y"},
        {"action": "send", "id": command_id, "message": "y"},
    ]
    waits = [
        {"action": "wait", "id": command_id, "timeout": 600},
        {"action": "await", "id": command_id},
        {"action": "poll_until", "id": command_id, "pattern": "ready"},
    ]

    for arguments in inputs:
        failure = error(await tasks.call(arguments, tools=tools))
        assert failure["code"] == "invalid_arguments"
        # terminal, which takes input, is named only when the Agent can call it.
        assert ("terminal" in failure["message"]) is terminal
    for arguments in waits:
        assert error(await tasks.call(arguments, tools=tools))["code"] == "invalid_arguments"
    message = error(await tasks.call({"action": "send", "id": "sub_child", "content": "hurry"}))
    assert (
        'subagent {"action": "send", "id": "sub_child", "content": "hurry"}' in (message["message"])
    )

    # Nothing was written or sent, nothing waited, and the command runs on.
    assert tasks.commands.clock.now == started_at
    assert tasks.commands.manager.latest_output(command_id, 5) == before
    assert tasks.commands.sink.submissions == []
    assert not tasks.commands.manager.report(command_id).ended
    assert process.tree.terminations == 0


@pytest.mark.asyncio
async def test_background_tasks_are_never_terminals(
    tasks: Tasks, manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    """User requirement: no shell command is ever a terminal, in either Tool."""
    terminals, factory = manager
    shell_context = dataclasses.replace(tasks.context(), tool_name=SHELL_TOOL_NAME)
    started = await dispatch_as_executor(
        tasks.registry,
        shell_context,
        {"command": "serve", "run_in_background": True},
        [SHELL_TOOL_NAME, BACKGROUND_TASKS_TOOL_NAME, "terminal"],
    )
    command_id = data(started)["command_id"]
    context = terminal_context(
        tmp_path, offered_tools=(SHELL_TOOL_NAME, BACKGROUND_TASKS_TOOL_NAME)
    )

    listed = await terminal_call(terminals, context, {"action": "list"})
    status = await terminal_call(
        terminals, context, {"action": "status", "terminal_id": command_id}
    )
    kill = await terminal_call(terminals, context, {"action": "kill", "terminal_id": command_id})
    refused = error(await tasks.call({"action": "status", "id": "term_abc123"}))

    assert command_id not in json.dumps(data(listed))
    assert terminals.list_terminals() == [] and factory.adapters == []
    for refusal in (error(status), error(kill)):
        assert refusal["code"] == "not_a_terminal"
        assert calls(refusal["message"]) == [{"action": "status", "id": command_id}]
    assert not tasks.commands.manager.report(command_id).ended
    assert refused["code"] == "not_a_background_task"
    assert 'terminal {"action": "status", "terminal_id": "term_abc123"}' in refused["message"]
