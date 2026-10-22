"""background_tasks: list, check and stop background commands and Sub-Agents.

It never gives input and never waits. Commands run on fake processes through a
real ``CommandManager`` on a fake clock (``command_manager_helpers``); a fake
Sub-Agent tree says which Sub-Agents work below the caller and in which
Sessions. Calls go through the registry as the executor sends them. The
Sub-Agent tree itself is tested over real Sessions in ``tests/core/subagents``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.permissions import PermissionBoundary
from core.runtime import _bootstrap
from core.runtime.runtime import Runtime
from core.sessions import SessionAddress
from core.subagents import SubAgentState, SubAgentStopOutcome
from core.tools import _command_output as command_output_module
from core.tools.availability import TOOL_ACTIVATION_FOLLOWS, ToolAccess, resolve_tool_access
from core.tools.background_tasks import (
    BACKGROUND_TASKS_TOOL_NAME,
    register_background_tasks_tool,
)
from core.tools.command_manager import CommandManager, NotRunReason
from core.tools.file_state import os_error_reason
from core.tools.model_names import called_tool_name
from core.tools.process_tree import RunningProcess
from core.tools.shell import SHELL_TOOL_NAME, format_command_delivery, register_shell_tool
from core.tools.tools import JsonObject, ToolContext, ToolRegistry
from core.utils.config import Config
from tests.core.tools.command_manager_helpers import (
    OWNER,
    FakeLauncher,
    FakeProcess,
    Harness,
    command_harness,
    format_report,
)
from tests.core.tools.terminal_helpers import make_context as terminal_context
from tests.core.tools.tools_test_support import dispatch_as_executor

# The caller's Sub-Agent, its own Sub-Agent, and a Session outside the caller's tree.
CHILD = SessionAddress("project-a", "sub-agent", "ses_child")
GRANDCHILD = SessionAddress("project-a", "sub-agent", "ses_grandchild")
SIBLING = SessionAddress("project-a", "sub-agent-2", "ses_sibling")
SERVER = RunningProcess(5001, "server.exe")
SUBAGENT = "subagent"


class FakeSubAgents:
    """The caller's Sub-Agent ``sub_child`` works in ``CHILD``, and nothing runs below it.

    Tests change its state, give it a Sub-Agent of its own, or name what it runs.
    """

    def __init__(self) -> None:
        self.child = SubAgentState(
            id="sub_child",
            title="Review docs",
            agent_id="sub-agent",
            session=CHILD,
            parent_id=None,
            status="working",
            last_tool="bash",
            activity_file="/tmp/sub_child.md",
        )
        self.grandchild: SubAgentState | None = None
        self.running: list[str] = []
        self.stopped: list[str] = []

    async def subagents(self, caller: SessionAddress) -> Sequence[SubAgentState]:
        if caller != OWNER:
            return ()
        return [self.child] if self.grandchild is None else [self.child, self.grandchild]

    async def running_work(self, subagent: SubAgentState) -> Sequence[str]:
        return self.running if subagent.id == self.child.id else ()

    async def stop_subagent(
        self, context: ToolContext, subagent: SubAgentState
    ) -> SubAgentStopOutcome:
        self.stopped.append(subagent.id)
        return "stopped" if subagent.status != "idle" or self.running else "idle"


class Tasks:
    """``background_tasks`` and the shell over one ``CommandManager`` with fakes."""

    def __init__(self, commands: Harness) -> None:
        self.commands = commands
        self.subagents = FakeSubAgents()
        self.registry = ToolRegistry()
        register_shell_tool(self.registry, commands.manager)
        register_background_tasks_tool(self.registry, commands.manager, subagents=self.subagents)
        # Callbacks the calls registered to run once their Tool Results are persisted.
        self.persisted: list[Callable[[], None]] = []
        self.contexts: list[ToolContext] = []

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
            permissions=PermissionBoundary.UNRESTRICTED,
        )

    async def call(
        self,
        arguments: Any,
        *,
        owner: SessionAddress = OWNER,
        tools: tuple[str, ...] = (SHELL_TOOL_NAME, "terminal", SUBAGENT),
    ) -> JsonObject:
        """One call as the executor sends it; a call that waits on time fails the test."""
        allowed = [BACKGROUND_TASKS_TOOL_NAME, *tools]
        context = self.context(owner)
        self.contexts.append(context)
        return await asyncio.wait_for(
            dispatch_as_executor(self.registry, context, arguments, allowed), 5
        )

    def shown(self, arguments: JsonObject, result: JsonObject) -> list[JsonObject]:
        """What the user sees of the last call."""
        display = self.registry.display_for_call(
            BACKGROUND_TASKS_TOOL_NAME, arguments, context=self.contexts[-1], result=result
        )
        details: list[JsonObject] = display["details"]
        return details

    async def background(
        self,
        *,
        owner: SessionAddress = OWNER,
        command: str = "serve",
        not_run_codes: dict[int, NotRunReason] | None = None,
    ) -> tuple[str, FakeProcess]:
        """A command of *owner* that continues in the background and delivers its result."""
        command_id, process = await self.commands.launch(
            owner=owner, command=command, formatter=format_report, not_run_codes=not_run_codes
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


# Command log files: tmp_<id>.log in Storage.
_OUTPUT_FILE = re.compile(r"(\S+\.log)(?=[\s.]|$)")


def output_file(text: str) -> Path:
    """The existing file with a command's complete output that *text* names."""
    match = _OUTPUT_FILE.search(text)
    assert match is not None, text
    path = Path(match[1])
    assert path.is_file()
    return path


# --- list --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shows_the_subagents_then_the_commands_of_this_session_and_its_subagents(
    tasks: Tasks,
) -> None:
    running, running_process = await tasks.background(command="serve")
    # A command on several lines is labelled on one line.
    ended, ended_process = await tasks.background(
        command='python -c "\nimport build\n\nbuild.main()"'
    )
    # The shell exits; a process it started runs on.
    ended_process.exit(1, leftovers=[SERVER])
    child, _ = await tasks.background(owner=CHILD, command="watch")
    sibling, _ = await tasks.background(owner=SIBLING, command="other")
    # A command still inside its call is no background task.
    await tasks.commands.launch(command="foreground")
    await tasks.commands.advance(60, step=5)

    tasks.subagents.grandchild = dataclasses.replace(
        tasks.subagents.child,
        id="sub_grandchild",
        title="Check links",
        session=GRANDCHILD,
        parent_id="sub_child",
        status="idle",
        last_tool=None,
    )

    listed = data(await tasks.call({"action": "list"}))

    subagents, rows = listed["tasks"][:2], listed["tasks"][2:]
    # Sub-Agents come first, in tree order.
    assert subagents == [
        {
            "id": "sub_child",
            "type": "subagent",
            "title": "Review docs",
            "agent_id": "sub-agent",
            "session_id": CHILD.session_id,
            "status": "working",
            "last_tool": "bash",
            "activity_file": "/tmp/sub_child.md",
        },
        {
            "id": "sub_grandchild",
            "type": "subagent",
            "title": "Check links",
            "agent_id": "sub-agent",
            "session_id": GRANDCHILD.session_id,
            "parent_id": "sub_child",
            "status": "idle",
            "activity_file": "/tmp/sub_child.md",
        },
    ]
    assert [row["id"] for row in rows] == [child, ended, running]
    for row in rows:
        assert row["type"] == "command"
        assert Path(row["log"]).is_file()
    assert [(row["command"], row["status"], row.get("subagent")) for row in rows] == [
        ("watch", "running", "sub_child"),
        ('python -c " import build build.main()"', "exited", None),
        ("serve", "running", None),
    ]
    assert (rows[1]["exit_code"], rows[1]["still_running"]) == (1, ["server.exe (pid 5001)"])
    assert "still_running" not in rows[2]
    assert rows[2]["runtime"].startswith("1 minute ")
    # Answers and results arrive by themselves: listing again is no way to wait.
    assert len(listed["notes"]) == 1
    # The user sees each listed task.
    shown = tasks.registry.display_for_call(
        BACKGROUND_TASKS_TOOL_NAME, {"action": "list"}, result={"ok": True, "data": listed}
    )["details"]
    assert [item["title"] for item in shown[0]["items"]] == [
        "Review docs",
        "Check links",
        "watch",
        'python -c " import build build.main()"',
        "serve",
    ]

    # A Sub-Agent sees its own commands, never its parent's or a sibling's.
    own = data(await tasks.call({"action": "list"}, owner=CHILD))["tasks"]
    assert [(row["id"], "subagent" in row) for row in own] == [(child, False)]
    assert sibling not in json.dumps(listed)

    empty = data(await tasks.call({"action": "list"}, owner=SessionAddress("p", "a", "s")))
    assert (empty["tasks"], len(empty["notes"])) == ([], 1)

    # A deeper Sub-Agent's answers go to its own Parent: with nothing of the caller's
    # own running, no message is to come.
    running_process.exit(0)
    await tasks.commands.finish(running)
    tasks.subagents.child = dataclasses.replace(tasks.subagents.child, status="idle")
    tasks.subagents.grandchild = dataclasses.replace(tasks.subagents.grandchild, status="working")
    assert "notes" not in data(await tasks.call({"action": "list"}))


# --- status ------------------------------------------------------------------


@pytest.mark.parametrize("owner", [OWNER, CHILD], ids=["own", "subagent"])
@pytest.mark.asyncio
async def test_status_shows_a_running_command_with_its_newest_output(
    tasks: Tasks, owner: SessionAddress
) -> None:
    command_id, process = await tasks.background(owner=owner)
    process.write("listening on 8000\n")
    await tasks.commands.advance(30, step=5)

    result = await tasks.call({"action": "status", "id": command_id})
    status = data(result)

    assert (status["status"], status["command_id"], status["output"]) == (
        "running",
        command_id,
        "listening on 8000",
    )
    # The call names only the id; the user sees which command it is.
    shown = tasks.shown({"action": "status", "id": command_id}, result)
    assert [
        item["title"] for block in shown if block["type"] == "results" for item in block["items"]
    ] == ["serve"]
    own = owner == OWNER
    assert status.get("subagent") == (None if own else "sub_child")
    # Its result goes to the Session that ran it; the Agent can stop it with the call named.
    assert ("sub_child" in status["next"]) is not own
    assert calls(status["next"]) == [{"action": "stop", "id": command_id}]
    output_file(status["next"])
    # Looking changes nothing.
    report = tasks.commands.manager.report(command_id)
    assert (report.ended, process.tree.terminations) == (False, 0)
    # From the third look on, the Session waiting for the result is told how often it looked.
    again = [data(await tasks.call({"action": "status", "id": command_id})) for _ in range(2)]
    assert "2 times" not in again[0]["next"]
    assert ("3 times" in again[1]["next"]) is own
    # Output that stopped arriving 5 minutes ago or more is named, so a stuck
    # command does not read as one making progress.
    assert "no new output" not in again[1]["next"]
    await tasks.commands.advance(300, step=60)
    silent = data(await tasks.call({"action": "status", "id": command_id}))
    assert "keeps running. It has printed no new output for 5 minutes." in silent["next"]


@pytest.mark.parametrize("owner", [OWNER, CHILD], ids=["own", "subagent"])
@pytest.mark.parametrize("exit_code", [0, 0xC0000005])
@pytest.mark.asyncio
async def test_status_of_an_ended_command_acknowledges_only_this_sessions_delivery(
    tasks: Tasks, owner: SessionAddress, exit_code: int
) -> None:
    command_id, process = await tasks.background(owner=owner)
    process.write("done\n")
    process.exit(exit_code)
    await tasks.commands.finish(command_id)
    # Submitted, not yet handed over to the Session.
    assert len(tasks.commands.sink.submissions) == 1

    listed = data(await tasks.call({"action": "list"}))["tasks"]
    [row] = [task for task in listed if task["id"] == command_id]
    result = await tasks.call({"action": "status", "id": command_id})
    status = data(result)
    tasks.persist()

    assert (status["status"], status["exit_code"], status["output"]) == (
        "exited",
        exit_code,
        "done",
    )
    hexadecimal = "0xC0000005" if exit_code == 0xC0000005 else None
    assert row.get("exit_code_hex") == status.get("exit_code_hex") == hexadecimal
    if hexadecimal:
        shown = tasks.shown({"action": "status", "id": command_id}, result)
        assert any(hexadecimal in block.get("text", "") for block in shown)
    output_file(status["next"])
    # The result showed the end to the Agent that waits for it; a Sub-Agent's stays.
    own = owner == OWNER
    assert tasks.commands.sink.cancellations == ([f"command:{command_id}"] if own else [])


@pytest.mark.asyncio
async def test_a_command_the_shell_ran_nothing_of_says_why_in_list_and_status(
    tasks: Tasks,
) -> None:
    # The shell exits with a code its launch named for running nothing.
    command_id, process = await tasks.background(not_run_codes={123456789: "request_unreadable"})
    process.exit(123456789)
    await tasks.commands.finish(command_id)

    listed = data(await tasks.call({"action": "list"}))["tasks"]
    [row] = [task for task in listed if task["type"] == "command"]
    result = await tasks.call({"action": "status", "id": command_id})
    status = data(result)
    tasks.persist()

    # No exit code, output or log file of the command's own.
    assert (row["status"], "exit_code" in row, "log" in row) == ("not_run", False, False)
    assert list(status) == ["status", "command_id", "not_run_because"]
    assert status["status"] == "not_run"
    assert "123456789" not in status["not_run_because"]
    # The status said why: the delivery that says it is withdrawn.
    assert tasks.commands.sink.cancellations == [f"command:{command_id}"]
    assert {"type": "notice", "level": "warning", "text": "The command did not run."} in (
        tasks.shown({"action": "status", "id": command_id}, result)
    )


_SUBAGENT_ID = re.compile(r"\bsub_[a-z0-9]+")


def other_subagents(text: str, subagent_id: str) -> list[str]:
    """The Sub-Agents *text* names besides *subagent_id*, outside the calls it names."""
    named = _SUBAGENT_ID.findall(_CALL.sub("", text))
    return [found for found in named if found != subagent_id]


@pytest.mark.parametrize(
    ("status", "deeper", "subagent_tool", "stops", "sends"),
    [
        # Its answer arrives by itself; stopping it is the one call to offer.
        ("working", False, True, True, False),
        # A deeper Sub-Agent works for its own Parent, which the text names.
        ("queued", True, True, True, False),
        ("idle", False, True, False, True),
        # Without subagent, the Agent cannot give it more work.
        ("idle", False, False, False, False),
        # Only its own Parent, which the text names, can message a deeper Sub-Agent.
        ("idle", True, True, False, False),
        # A user-directed answer does not change available controls.
        ("user", False, True, True, True),
    ],
    ids=[
        "working",
        "queued-deeper",
        "idle",
        "idle-without-subagent",
        "idle-deeper",
        "answer-to-user",
    ],
)
@pytest.mark.asyncio
async def test_status_of_a_subagent_says_what_the_caller_can_do_about_it(
    tasks: Tasks,
    status: Any,
    deeper: bool,
    subagent_tool: bool,
    stops: bool,
    sends: bool,
) -> None:
    answers_user = status == "user"
    status = "working" if answers_user else status
    subagent = dataclasses.replace(
        tasks.subagents.child, status=status, reply_target="user" if answers_user else "parent"
    )
    if deeper:
        subagent = dataclasses.replace(
            subagent, id="sub_grandchild", session=GRANDCHILD, parent_id="sub_child"
        )
        tasks.subagents.grandchild = subagent
    else:
        tasks.subagents.child = subagent
        tasks.subagents.running = ["background command cmd_a (serve)"]
    tools = (SHELL_TOOL_NAME, SUBAGENT) if subagent_tool else (SHELL_TOOL_NAME,)

    shown = data(await tasks.call({"action": "status", "id": subagent.id}, tools=tools))

    # The call it offers, the Tool that gives it more work, and the Parent of a deeper one.
    assert calls(shown["next"]) == ([{"action": "stop", "id": subagent.id}] if stops else [])
    assert (SUBAGENT in _CALL.sub("", shown["next"])) is sends
    assert other_subagents(shown["next"], subagent.id) == (["sub_child"] if deeper else [])
    assert (shown["id"], shown["type"], shown["status"]) == (subagent.id, "subagent", status)
    assert shown["project_id"] == "project-a"
    # Only a working turn has a last Tool; only work it still runs for the caller is named.
    assert ("last_tool" in shown) is (status == "working")
    running = None if deeper else ["background command cmd_a (serve)"]
    assert shown.get("running") == running
    if answers_user:
        assert shown["reply_to"] == "user"
        looked = data(await tasks.call({"id": subagent.id}, tools=tools))
        assert calls(" ".join(looked["notes"])) == [{"action": "stop", "id": subagent.id}]
    assert tasks.subagents.stopped == []


@pytest.mark.parametrize(
    ("status", "deeper", "subagent_tool", "continues", "answers_user"),
    [
        ("working", False, True, True, False),
        ("working", True, True, True, False),
        ("working", False, False, False, False),
        ("idle", False, True, False, False),
        ("working", False, True, True, True),
    ],
)
@pytest.mark.asyncio
async def test_stop_of_a_subagent_stops_its_tree_and_an_idle_one_is_no_failure(
    tasks: Tasks,
    status: Any,
    deeper: bool,
    subagent_tool: bool,
    continues: bool,
    answers_user: bool,
) -> None:
    working = status != "idle"
    subagent = dataclasses.replace(
        tasks.subagents.child, status=status, last_tool="bash" if status == "working" else None
    )
    subagent = dataclasses.replace(subagent, reply_target="user" if answers_user else "parent")
    if deeper:
        subagent = dataclasses.replace(
            subagent, id="sub_grandchild", session=GRANDCHILD, parent_id="sub_child"
        )
        tasks.subagents.grandchild = subagent
    else:
        tasks.subagents.child = subagent
    tools = (SHELL_TOOL_NAME, SUBAGENT) if subagent_tool else (SHELL_TOOL_NAME,)

    result = await tasks.call({"action": "stop", "id": subagent.id}, tools=tools)

    stopped = data(result)
    assert tasks.subagents.stopped == [subagent.id]
    (note,) = stopped.pop("notes")
    assert stopped == {
        "id": subagent.id,
        "type": "subagent",
        "agent_id": "sub-agent",
        "session_id": subagent.session.session_id,
        "project_id": "project-a",
        "status": "stopped" if working else "idle",
    }
    # The note names a message as the way to continue only where the caller can have one
    # sent, and the Parent that sends it to a deeper Sub-Agent.
    assert ("send" in note) is continues
    assert other_subagents(note, subagent.id) == (["sub_child"] if continues and deeper else [])
    shown = tasks.registry.display_for_call(
        BACKGROUND_TASKS_TOOL_NAME, {"action": "stop", "id": subagent.id}, result=result
    )["details"]
    assert shown == [
        {
            "type": "notice",
            "level": "info",
            "text": "The Sub-Agent was stopped."
            if working
            else "The Sub-Agent was idle; nothing needed stopping.",
        }
    ]


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
    # Its exit code is vBot's kill, not the command's.
    assert "exit_code" not in stopped
    output_file(stopped["next"])
    assert process.tree.terminations == 1
    if owner == OWNER:
        # The stop result is the outcome: nothing is delivered.
        assert (report.stop_reason, tasks.commands.sink.submissions) == ("agent", [])
        assert "notes" not in stopped
    else:
        # The Sub-Agent did not see the stop: its Session gets the result.
        (submission,) = tasks.commands.sink.submissions
        assert (report.stop_reason, submission["address"]) == ("parent", CHILD)
        assert "sub_child" in stopped["notes"][0]
        await _later_reads_speak_to_their_reader(tasks, command_id, stopped)


@pytest.mark.parametrize("before", ["", "collecting\n"], ids=["nothing", "a statement"])
@pytest.mark.asyncio
async def test_a_stop_does_not_call_the_log_complete_when_a_pipe_stage_held_output(
    tasks: Tasks, before: str
) -> None:
    stage = "Select-Object -Last 25"
    command_id, process = await tasks.background(
        command=f"Write-Output collecting; python -m unittest 2>&1 | {stage}"
    )
    process.write(before)

    stopped = data(await tasks.call({"action": "stop", "id": command_id}))
    await tasks.commands.finish(command_id)

    # The lines the stage held never reached vBot: the hint names the stage, and
    # the log file the result names holds only what arrived, if anything did.
    assert stage in stopped["hint"]
    assert stopped.get("output") == (before.strip() or None)
    if before:
        output_file(stopped["next"])
        assert "complete" not in stopped["next"]
    else:
        assert "next" not in stopped


@pytest.mark.parametrize(
    ("prefix", "kept", "stop"),
    [
        (b"first\n", 6, False),
        (b"first\rsecond\rthird\r", 19, False),
        ("ü".encode(), 2, False),
        (b"x", 1, False),
        (b"", 0, False),
        (b"first\n", 6, True),
    ],
    ids=["lf", "cr-only", "utf-8", "one-byte", "nothing", "stopped"],
)
@pytest.mark.asyncio
async def test_a_log_file_vbot_could_not_write_is_never_called_complete(
    tasks: Tasks, monkeypatch: pytest.MonkeyPatch, prefix: bytes, kept: int, stop: bool
) -> None:
    full = OSError(errno.ENOSPC, "No space left on device")
    room = [kept]

    def full_disk(fd: int, data: bytes) -> tuple[int, OSError | None]:
        # The disk takes the first *kept* bytes of the output and nothing more.
        count = os.write(fd, data[: room[0]]) if room[0] else 0
        room[0] -= count
        return (count, None) if count == len(data) else (count, full)

    monkeypatch.setattr(command_output_module, "_write_all", full_disk)
    command_id, process = await tasks.background()
    path = tasks.commands.manager.report(command_id).log_path
    assert path is not None
    process.write(prefix + "".join(f"line {number}\n" for number in range(5_000)).encode())
    await tasks.commands.advance(5, step=1)

    def explained(text: str, *, named: bool = True) -> None:
        # Once, with why; the file named only when it holds something.
        assert text.count(os_error_reason(full)) == 1, text
        assert "complete" not in text, text
        assert (path.name in text) is (named and kept > 0), text
        if kept:
            assert f"{kept:,} byte" in text, text

    def safe_repeat(text: str) -> None:
        # Losing the log never makes an already executed effect safe to repeat.
        assert "only if" in text and "effects" in text and "safe" in text, text

    listed = data(await tasks.call({"action": "list"}))["tasks"]
    [row] = [task for task in listed if task["type"] == "command"]
    assert ("log" in row) is (kept > 0)
    explained(row["log_incomplete"], named=False)
    running = data(await tasks.call({"action": "status", "id": command_id, "filter": "error"}))
    explained(running["next"])
    explained(" ".join(running["notes"]))
    safe_repeat(running["next"])
    assert "interrupting" in running["next"]
    assert process.tree.terminations == 0
    if stop:
        stopped = data(await tasks.call({"action": "stop", "id": command_id}))
        explained(json.dumps(stopped))
        safe_repeat(json.dumps(stopped))
    else:
        process.exit(1)
    report = await tasks.commands.finish(command_id)
    ended = json.dumps(data(await tasks.call({"action": "status", "id": command_id})))
    delivery = format_command_delivery(report, permissions=PermissionBoundary.UNRESTRICTED)
    explained(ended)
    explained(delivery)
    safe_repeat(ended)
    safe_repeat(delivery)
    assert path.read_bytes() == prefix[:kept]


async def _later_reads_speak_to_their_reader(
    tasks: Tasks, parent_stopped: str, fresh: JsonObject
) -> None:
    """Why a Sub-Agent's commands were stopped, read later by it and by its Parent.

    The Parent stopped *parent_stopped* and got *fresh*; the Sub-Agent stops a
    second command itself.
    """
    own_stopped, _ = await tasks.background(owner=CHILD)
    await tasks.call({"action": "stop", "id": own_stopped}, owner=CHILD)
    await tasks.commands.finish(own_stopped)

    async def reason(command_id: str, reader: SessionAddress) -> str:
        shown = data(await tasks.call({"action": "status", "id": command_id}, owner=reader))
        because: str = shown["stopped_because"]
        return because

    parent_stop = {reader: await reason(parent_stopped, reader) for reader in (OWNER, CHILD)}
    child_stop = {reader: await reason(own_stopped, reader) for reader in (OWNER, CHILD)}
    again = data(await tasks.call({"action": "stop", "id": parent_stopped}))

    # The Parent reads both as stops in sub_child's Session and tells them apart; neither
    # says that the Parent itself stopped it, which only the fresh result knows.
    assert "sub_child" in parent_stop[OWNER] and "sub_child" in child_stop[OWNER]
    assert len({parent_stop[OWNER], child_stop[OWNER], fresh["stopped_because"]}) == 3
    # A repeated stop stops nothing and reads the stop like a later status.
    assert again["stopped_because"] == parent_stop[OWNER]
    # The Sub-Agent's own reads speak to it: its own stop reads like the Parent's fresh one.
    assert child_stop[CHILD] == fresh["stopped_because"]
    assert parent_stop[CHILD] != child_stop[CHILD]
    assert not any("sub_child" in text for text in (parent_stop[CHILD], child_stop[CHILD]))


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
async def test_an_unconfirmed_stop_names_what_still_ran_and_how_to_retry(
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
    ("arguments", "outcome", "ignored"),
    [
        ({"action": "kill", "task_id": "{id}"}, "stopped", ()),
        ({"action": "output", "bash_id": "{id}"}, "running", ()),
        ({"action": "status", "terminal_id": "{id}"}, "running", ()),
        ({"action": "status", "id": ["{id}"]}, "running", ()),
        ({"action": "ls", "id": "{id}"}, "listed", ()),
        # Without an action an id is only looked at, also under a harness's name.
        ({"shell_id": "{id}"}, "looked-at", ()),
        # Claude Code's TaskOutput and BashOutput: nothing waits for a task or filters
        # its output, and the call says so, naming the fields of each note.
        (
            {"task_id": "{id}", "block": True, "timeout": 30000},
            "looked-at",
            (("block", "timeout"),),
        ),
        ({"task_id": "{id}", "block": False, "timeout": 30000}, "looked-at", ()),
        ({"bash_id": "{id}", "filter": "error"}, "looked-at", (("filter",),)),
        ({"task_id": "sub_child", "filter": "error"}, "subagent-looked-at", (("filter",),)),
        ({"action": "status", "id": "{id}", "wait": True}, "running", (("wait",),)),
    ],
    ids=[
        "kill-task_id",
        "output-bash_id",
        "terminal_id",
        "id-list",
        "ls",
        "shell_id-without-action",
        "TaskOutput-block",
        "TaskOutput-no-block",
        "BashOutput-filter",
        "TaskOutput-filter-subagent",
        "wait-field",
    ],
)
@pytest.mark.asyncio
async def test_other_harnesses_shapes_reach_the_intended_action(
    tasks: Tasks, arguments: JsonObject, outcome: str, ignored: tuple[tuple[str, ...], ...]
) -> None:
    command_id, process = await tasks.background()
    sent = json.loads(json.dumps(arguments).replace("{id}", command_id))
    # Claude Code's KillShell is this Tool.
    name = called_tool_name("KillShell", [SHELL_TOOL_NAME, BACKGROUND_TASKS_TOOL_NAME])
    assert name == BACKGROUND_TASKS_TOOL_NAME

    result = data(await tasks.call(sent))

    if outcome == "listed":
        assert [row["id"] for row in result["tasks"]] == ["sub_child", command_id]
        return
    subagent = outcome == "subagent-looked-at"
    task_id = "sub_child" if subagent else command_id
    assert result.get("id", result.get("command_id")) == task_id
    assert result["status"] == (
        "working" if subagent else "stopped" if outcome == "stopped" else "running"
    )
    assert process.tree.terminations == (1 if outcome == "stopped" else 0)
    notes = result.get("notes", [])
    if outcome.endswith("looked-at"):
        # The first note names the stop the Agent may have meant.
        assert calls(notes[0]) == [{"action": "stop", "id": task_id}]
        notes = notes[1:]
    assert len(notes) == len(ignored)
    for note, fields in zip(notes, ignored, strict=True):
        assert all(field in note for field in fields), note
    if subagent:
        # What the Sub-Agent did is in its activity log, where the Agent can search it.
        assert "/tmp/sub_child.md" in notes[0]
    elif "filter" in sent:
        # The complete output is where the Agent can search it.
        output_file(notes[0])


@pytest.mark.asyncio
async def test_a_subagents_session_id_names_that_subagent(tasks: Tasks) -> None:
    shown = data(await tasks.call({"action": "status", "session_id": CHILD.session_id}))
    unknown = error(await tasks.call({"action": "stop", "id": SIBLING.session_id}))
    # A message for it is refused with the subagent call that sends it.
    message = error(
        await tasks.call({"action": "send", "id": CHILD.session_id, "content": "hurry"})
    )
    unknown_message = error(
        await tasks.call({"action": "send", "id": SIBLING.session_id, "content": "hurry"})
    )

    assert shown["id"] == "sub_child"
    assert shown["notes"] == [f"session_id {CHILD.session_id} was read as Sub-Agent sub_child."]
    assert unknown["code"] == "task_not_found"
    assert tasks.subagents.stopped == []
    assert message["code"] == "invalid_arguments"
    sent = message["message"]
    assert 'subagent {"action": "send", "id": "sub_child", "content": "hurry"}' in sent
    # A Session of no Sub-Agent of the caller names no call that cannot work.
    assert unknown_message["code"] == "invalid_arguments"
    assert SUBAGENT in unknown_message["message"]
    assert SIBLING.session_id not in unknown_message["message"]
    assert "{" not in unknown_message["message"]


@pytest.mark.asyncio
async def test_ids_outside_this_session_and_its_subagents_are_refused(tasks: Tasks) -> None:
    own, _ = await tasks.background(command="serve")
    sibling, sibling_process = await tasks.background(owner=SIBLING)
    foreground, _ = await tasks.commands.launch(command="foreground")
    # Its shell exited, but a process it started runs on.
    left, left_process = await tasks.background(command="start-server")
    left_process.exit(0, leftovers=[SERVER])
    await tasks.commands.advance(4)
    assert tasks.commands.manager.report(left).ended
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
        # The refusal names what of the caller's runs now, working Sub-Agents first.
        assert (
            f"Running now: sub_child (Review docs), {left} (start-server), {own} (serve)."
            in failure["message"]
        )
    assert error(refusals[3])["code"] == "task_not_found"
    assert missing["code"] == "invalid_arguments" and f"{own} (serve)" in missing["message"]
    assert several["code"] == "invalid_arguments"
    assert sibling_process.tree.terminations == 0
    assert not tasks.commands.manager.report(own).ended


# --- Availability ----------------------------------------------------------------


def test_background_tasks_follows_the_shell_and_subagent() -> None:
    commands = CommandManager()
    tools = ToolRegistry()
    register_shell_tool(tools, commands)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    tools.register(SUBAGENT, "Delegate tasks.", schema, lambda _context, _arguments: {})
    tool = register_background_tasks_tool(tools, commands)
    registered = tools.get(BACKGROUND_TASKS_TOOL_NAME)

    # How a followed Tool resolves is availability's contract; this is its registration.
    assert (registered.activation, registered.activation_source) == (
        TOOL_ACTIVATION_FOLLOWS,
        (SHELL_TOOL_NAME, SUBAGENT),
    )
    assert registered.handler is tool
    allowed = resolve_tool_access(
        ToolAccess(mode="selected", allowed=(SUBAGENT,)), tools.list_tools(), "off"
    ).allowed_tools
    assert set(allowed) == {SUBAGENT, BACKGROUND_TASKS_TOOL_NAME}
    # The user configures the shell and subagent; their companion is no switch of its own.
    assert [tool.name for tool in tools.list_tools(include_catalog_hidden=False)] == [
        SHELL_TOOL_NAME,
        SUBAGENT,
    ]


# --- User requirement --------------------------------------------------------------


@pytest.mark.parametrize("callable_tools", [True, False], ids=["terminal-subagent", "neither"])
@pytest.mark.asyncio
async def test_background_tasks_sends_no_input_and_never_waits(
    tasks: Tasks, callable_tools: bool
) -> None:
    """User requirement: background_tasks never gives a command input and never waits."""
    command_id, process = await tasks.background()
    process.write("Continue? [y/N] ")
    await tasks.commands.advance(1)
    before = tasks.commands.manager.latest_output(command_id, 5)
    started_at = tasks.commands.clock.now
    tools = (SHELL_TOOL_NAME, "terminal", SUBAGENT) if callable_tools else (SHELL_TOOL_NAME,)
    inputs: list[JsonObject] = [
        {"action": "input", "id": command_id, "text": "y\n"},
        {"action": "send_keys", "id": command_id, "keys": ["Enter"]},
        {"action": "write", "id": command_id, "content": "y"},
        {"action": "stdin", "id": command_id, "input": "y"},
        {"action": "send", "id": command_id, "message": "y"},
    ]
    waits: list[JsonObject] = [
        {"action": "wait", "id": command_id, "timeout": 600},
        {"action": "await", "id": command_id},
        {"action": "poll_until", "id": command_id, "pattern": "ready"},
    ]
    # Fields that ask to wait, as Claude Code's TaskOutput sends them, have no effect.
    wait_fields: list[JsonObject] = [
        {"action": "status", "id": command_id, "block": True, "timeout": 600},
        {"task_id": command_id, "block": True, "timeout": 30000},
    ]

    async def answered(arguments: JsonObject) -> JsonObject:
        """The call's result; fake time passes only for a call that waits."""
        call = asyncio.ensure_future(tasks.call(arguments, tools=tools))
        await asyncio.wait({call}, timeout=1)
        while not call.done():
            await tasks.commands.advance(5, step=5)
        return call.result()

    for arguments in inputs:
        failure = error(await answered(arguments))
        assert failure["code"] == "invalid_arguments"
        # terminal, which takes input, is named only when the Agent can call it.
        assert ("terminal" in failure["message"]) is callable_tools
    for arguments in waits:
        assert error(await answered(arguments))["code"] == "invalid_arguments"
    for arguments in wait_fields:
        assert data(await answered(arguments))["status"] == "running"
    message = error(await answered({"action": "send", "id": "sub_child", "content": "hurry"}))
    # subagent, which sends messages, is named only when the Agent can call it.
    assert (
        'subagent {"action": "send", "id": "sub_child", "content": "hurry"}' in message["message"]
    ) is callable_tools
    assert ("subagent" in message["message"]) is callable_tools

    # Nothing was written or sent, no call waited for fake time, and the command runs on.
    assert tasks.commands.clock.now == started_at
    assert tasks.commands.manager.latest_output(command_id, 5) == before
    assert tasks.commands.sink.submissions == []
    assert not tasks.commands.manager.report(command_id).ended
    assert process.tree.terminations == 0


@pytest.mark.asyncio
async def test_background_commands_are_never_terminals(
    tmp_path: Path, current_format_data_directory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User requirement: no shell command is ever a terminal, in either Tool.

    Checked on the Runtime's own wiring, where the shell, background_tasks and
    terminal share one Runtime; only the command's process is a fake.
    """
    launcher = FakeLauncher()
    monkeypatch.setattr(_bootstrap, "CommandManager", partial(CommandManager, launcher=launcher))
    runtime = Runtime(Config(data_dir=tmp_path), safe_startup_mode="test")
    runtime.start()
    try:
        runtime.chat_sessions.create(OWNER.agent_id, OWNER.session_id, OWNER.project_id)
        allowed = [SHELL_TOOL_NAME, BACKGROUND_TASKS_TOOL_NAME, "terminal"]

        async def call(tool_name: str, arguments: JsonObject) -> JsonObject:
            context = dataclasses.replace(
                terminal_context(tmp_path, offered_tools=allowed), tool_name=tool_name
            )
            return await dispatch_as_executor(runtime.tools, context, arguments, allowed)

        # Asked for a terminal, the command still runs as a plain process.
        started = data(
            await call(
                SHELL_TOOL_NAME, {"command": "serve", "run_in_background": True, "pty": True}
            )
        )
        command_id = started["command_id"]
        listed = data(await call("terminal", {"action": "list"}))
        status = error(await call("terminal", {"action": "status", "terminal_id": command_id}))
        kill = error(await call("terminal", {"action": "kill", "terminal_id": command_id}))
        refused = error(
            await call(BACKGROUND_TASKS_TOOL_NAME, {"action": "status", "id": "term_abc123"})
        )

        assert len(launcher.processes) == 1
        assert command_id not in json.dumps(listed)
        assert runtime.terminal_manager.list_terminals() == []
        for refusal, action in ((status, "status"), (kill, "stop")):
            assert refusal["code"] == "not_a_terminal"
            assert calls(refusal["message"]) == [{"action": action, "id": command_id}]
        assert not runtime.command_manager.report(command_id).ended
        assert refused["code"] == "not_a_background_task"
        assert 'terminal {"action": "status", "terminal_id": "term_abc123"}' in refused["message"]
    finally:
        await runtime.aclose()
