"""Context commands: the note a Session's first Run gets with their outputs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, override

import pytest

from core.automation.cron import WatchCheckError
from core.chat import ChatMessage
from core.chat._context_commands import CONTEXT_COMMANDS_META_KEY, CONTEXT_OUTPUT_MAX_CHARS
from core.permissions import PermissionBoundary
from core.projects import ProjectStore
from core.runs import RunCancelledError
from core.tools import ToolRegistry
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubRuntime,
    build_chat_loop,
    session_address,
)

PROJECT = "acme"


class _Runner:
    """Answers each command from a script and records where it ran."""

    def __init__(self, answers: dict[str, tuple[int, str] | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, Path]] = []

    async def run_context_command(
        self, command: str, *, workdir: Path, boundary: PermissionBoundary
    ) -> tuple[int, str]:
        self.calls.append((command, workdir))
        answer = self.answers[command]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _runtime(tmp_path: Path, runner: _Runner, *, agent_commands: list[str]) -> Any:
    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        workspace=tmp_path / "workspace",
        context_commands=tuple(agent_commands),
    )
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=StubAdapter([{"content": "Done", "tool_calls": None}] * 10),
        tools=ToolRegistry(),
    )
    runtime.context_command_runner = runner
    (tmp_path / "repo").mkdir()
    runtime.projects = ProjectStore(tmp_path)
    runtime.projects.create(PROJECT, "Acme", tmp_path / "repo")
    return runtime


def _context_notes(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [
        message
        for message in messages
        if message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith("When this Session started")
    ]


def _record(note: ChatMessage) -> list[dict[str, Any]]:
    assert isinstance(note.content, str)
    return list(json.loads(note.content.splitlines()[-1]))


@pytest.mark.asyncio
async def test_the_first_run_of_a_session_gets_one_note_with_every_output(tmp_path: Path) -> None:
    long_output = "<b>row</b>\n" * 1000
    runner = _Runner(
        {
            "git status --short": (0, "M core/app.py\r\n\n"),
            "Get-Content log.txt": (1, long_output),
            "Get-Content missing.txt": WatchCheckError("The command could not start: no shell."),
        }
    )
    runtime = _runtime(
        tmp_path,
        runner,
        agent_commands=["git status --short", "Get-Content log.txt", "Get-Content missing.txt"],
    )
    session = runtime.chat_sessions.create("coder", session_id="source")
    loop = build_chat_loop(runtime)
    for _ in range(2):
        run = await loop.start_run("coder", "Work", session_id=session.id)
        await run.wait()

    # Once, in the Workspace, after the first input and before the first answer.
    assert [workdir for _command, workdir in runner.calls] == [tmp_path / "workspace"] * 3
    messages = session.load_active()
    [note] = _context_notes(messages)
    assert isinstance(note.content, str)
    assert [message.role for message in messages[:3]] == ["user", "note", "assistant"]
    assert runtime.chat_sessions.metadata_value(session.address, CONTEXT_COMMANDS_META_KEY) == {
        "note_id": note.id
    }
    assert any(
        note.content in str(message.get("content", ""))
        for message in runtime.adapter.requests[0]["messages"]
    )

    # Outputs are quoted data sharing the room equally; a failure says why the command did not run.
    assert "for your new Sessions in your Workspace" in note.content
    assert "<b>" not in note.content
    status, log, missing = _record(note)
    assert status == {"command": "git status --short", "exit_code": 0, "output": "M core/app.py"}
    share = CONTEXT_OUTPUT_MAX_CHARS // 2
    assert (log["exit_code"], log["cut"]) == (1, True)
    assert share // 2 < len(log["output"]) <= share
    assert log["output"].endswith("</b>")
    assert f"at most {share} characters" in note.content
    assert missing == {
        "command": "Get-Content missing.txt",
        "error": "The command could not start: no shell",
    }

    # A fork carries the conversation, so it starts no Session of its own.
    fork = await runtime.chat_sessions.fork(session.address)
    run = await loop.start_run("coder", "Continue", session_id=fork.id)
    await run.wait()
    assert len(runner.calls) == 3
    assert len(_context_notes(fork.load_active())) == 1


@pytest.mark.asyncio
async def test_a_session_in_a_project_runs_only_the_projects_commands(tmp_path: Path) -> None:
    runner = _Runner({"git status": (0, "clean"), "agent command": (0, "")})
    runtime = _runtime(tmp_path, runner, agent_commands=["agent command"])
    loop = build_chat_loop(runtime)

    # Without commands of its own, the Project runs none, not the Agent's.
    await loop.send("coder", "Hi", session_id="empty", project_id=PROJECT)
    assert runner.calls == []

    runtime.projects.update(PROJECT, context_commands=["git status"])
    await loop.send("coder", "Hi", session_id="session-one", project_id=PROJECT)
    assert runner.calls == [("git status", (tmp_path / "repo").resolve())]
    session = runtime.chat_sessions.get(session_address("coder", "session-one", PROJECT))
    [note] = _context_notes(session.load_active())
    assert isinstance(note.content, str)
    assert "for new Sessions in this Project" in note.content


@pytest.mark.asyncio
async def test_a_stop_before_the_note_ends_the_commands_and_leaves_them_to_the_next_run(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    ended: list[str] = []

    class _Waiting(_Runner):
        @override
        async def run_context_command(
            self, command: str, *, workdir: Path, boundary: PermissionBoundary
        ) -> tuple[int, str]:
            if self.calls:
                return await super().run_context_command(
                    command, workdir=workdir, boundary=boundary
                )
            self.calls.append((command, workdir))
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.append(command)
            raise AssertionError("unreachable")

    runner = _Waiting({"git status": (0, "clean")})
    runtime = _runtime(tmp_path, runner, agent_commands=["git status"])
    session = runtime.chat_sessions.create("coder", session_id="source")
    loop = build_chat_loop(runtime)

    run = await loop.start_run("coder", "Work", session_id=session.id)
    await started.wait()
    # The Run persists its input while the commands run; the Stop comes after it.
    for _ in range(500):
        if any(message.role == "user" for message in session.load_active()):
            break
        await asyncio.sleep(0.01)
    run.request_cancel(reason="user")
    with pytest.raises(RunCancelledError):
        await run.wait()
    assert ended == ["git status"]
    stopped = session.load_active()
    assert [message.content for message in stopped if message.role == "user"] == ["Work"]
    assert _context_notes(stopped) == []

    run = await loop.start_run("coder", "Work again", session_id=session.id)
    await run.wait()
    messages = session.load_active()
    [note] = _context_notes(messages)
    before = messages[messages.index(note) - 1]
    assert (before.role, before.content) == ("user", "Work again")
    assert _record(note) == [{"command": "git status", "exit_code": 0, "output": "clean"}]
