"""Contract tests for Session goals: ``/goal``, automatic Runs and their pauses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from core.automation.goals import (
    GOAL_CONTINUE_TEXT,
    GOAL_LAST_TURN_TEXT,
    GOAL_RESUMED_TEXT,
    GoalService,
)
from core.chat import ChatSessionManager, ReplySurface
from core.chat.commands import CommandExecutionContext
from core.chat.messages import ChatMessage
from core.runs import ChatRunManager, Run, RunAdmission, RunKind
from core.sessions import SESSION_GOAL_META_KEY, SessionAddress

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]

_ADDRESS = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
Turn = Callable[[Run], Awaitable[None]]


async def _works(_run: Run) -> None:
    """A turn that only answers: a goal continues without a Tool call."""


class _GoalChatLoop:
    """Start each Run with the next scripted turn; unscripted turns do work.

    Like Chat, a Run stores its context note in the History before it works.
    """

    def __init__(self, manager: ChatRunManager, sessions: ChatSessionManager) -> None:
        self._manager = manager
        self._sessions = sessions
        self.turns: list[Turn] = []
        self.started: list[tuple[str, RunKind]] = []
        # (internal, context note) of each started Run.
        self.inputs: list[tuple[bool, ChatMessage | None]] = []

    async def start_run(
        self,
        agent_id: str,
        content: str,
        *,
        session_id: str,
        internal: bool,
        reply_surface: ReplySurface | None,
        project_id: str | None,
        run_kind: RunKind,
        sender: Any = None,
        context_note: ChatMessage | None = None,
    ) -> Run:
        turn = self.turns.pop(0) if self.turns else _works
        address = SessionAddress(project_id, agent_id, session_id)

        async def executor(run: Run) -> str:
            run.internal = internal
            self.started.append((content, run_kind))
            self.inputs.append((internal, context_note))
            if context_note is not None:
                self._sessions.get(address).add_note_message(context_note)
            await turn(run)
            return "done"

        return await self._manager.start(
            address, executor, admission=RunAdmission(run_kind=run_kind)
        )


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.runs = ChatRunManager()
        self.sessions = ChatSessionManager(tmp_path)
        self.sessions.create("coder", session_id="session-one")
        self.loop = _GoalChatLoop(self.runs, self.sessions)
        self.work_pending = False
        # (Session, whether the change appended a goal row outside a Run)
        self.changes: list[tuple[SessionAddress, bool]] = []

        async def pending(_address: SessionAddress) -> bool:
            return self.work_pending

        self.goals = GoalService(
            sessions=self.sessions,
            run_manager=self.runs,
            chat_loop=cast(Any, self.loop),
            work_pending=pending,
        )
        self.goals.install()
        self.goals.add_changed_callback(
            lambda address, history: self.changes.append((address, history))
        )

    async def command(self, argument: str | None) -> str:
        outcome = await self.goals.execute_command(
            CommandExecutionContext(
                agent_id="coder",
                session_id="session-one",
                project_id=None,
                reply_surface=ReplySurface.webui(),
                command_text=f"/goal {argument}" if argument else "/goal",
            ),
            argument,
        )
        return outcome.feedback.text if outcome.feedback is not None else ""

    def goal(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY))

    def notes(self) -> list[str]:
        return [
            cast(str, message.content)
            for message in self.sessions.get(_ADDRESS).load()
            if message.role == "note" and message.content
        ]

    def rows(self) -> list[dict[str, Any]]:
        """The goal rows of the History, in order."""
        return [
            message.goal_event.to_dict()
            for message in self.sessions.get(_ADDRESS).load()
            if message.goal_event is not None
        ]

    async def settle(self) -> None:
        """Wait until no Run is active and every goal decision has run."""
        while True:
            runs = self.runs.active_runs()
            # A completed Task may still await its discard callback. Gathering
            # only completed Tasks returns eagerly and starves that callback.
            decisions = [task for task in self.goals._tasks if not task.done()]  # noqa: SLF001
            if not runs and not decisions:
                return
            # A decision may start another Run, whose completion spawns another
            # decision. Wait for each snapshot instead of racing Session workers.
            await asyncio.gather(*(run.wait() for run in runs), *decisions, return_exceptions=True)

    async def user_run(self, turn: Turn = _works) -> None:
        """A message the user sends: a visible user Run."""

        async def executor(run: Run) -> str:
            await turn(run)
            return "answer"

        await self.runs.start(_ADDRESS, executor)


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    harness = _Harness(tmp_path)
    try:
        yield harness
    finally:
        await harness.goals.aclose()
        await harness.runs.aclose()
        harness.sessions.close()


def _marks(goals: GoalService, status: Any) -> Turn:
    async def turn(run: Run) -> None:
        await goals.update_from_agent(_ADDRESS, status, run_id=run.id)

    return turn


async def test_goal_runs_automatically_until_the_agent_marks_it_complete(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.loop.turns = [_works, _works, _marks(harness.goals, "complete")]

    await harness.command("Ship <the> release")
    read_started, release_read = asyncio.Event(), asyncio.Event()
    metadata_value_async = harness.sessions.metadata_value_async

    async def held_read(address: SessionAddress, key: str) -> Any:
        read_started.set()
        await release_read.wait()
        return await metadata_value_async(address, key)

    monkeypatch.setattr(harness.sessions, "metadata_value_async", held_read)
    async with asyncio.TaskGroup() as tasks:
        settling = tasks.create_task(harness.settle())
        try:
            await read_started.wait()
            assert not settling.done()
        finally:
            release_read.set()

    first, *automatic = harness.loop.started
    # The user's command is the first Run's visible input; its note tells the
    # Model, the objective quoted as user text.
    assert first == ("/goal Ship <the> release", RunKind.USER)
    internal, set_note = harness.loop.inputs[0]
    assert not internal and set_note is not None
    assert cast(str, set_note.content).startswith(
        "The user set a goal for this session with /goal:\n"
    )
    assert '"Ship \\u003cthe\\u003e release"' in cast(str, set_note.content)
    assert automatic == [(GOAL_CONTINUE_TEXT, RunKind.SYSTEM)] * 2
    assert harness.goal()["status"] == "complete"
    assert harness.goal()["turns"] == 2
    assert await harness.command(None) == (
        "Goal (complete): Ship <the> release\nAutomatic turns: 2 of 30."
    )
    # Each change is a row of the conversation; the Agent's completion follows its Run.
    assert harness.rows() == [
        {"event": "set", "objective": "Ship <the> release", "max_turns": 30},
        {"event": "turn", "turn": 1, "max_turns": 30},
        {"event": "turn", "turn": 2, "max_turns": 30},
        {"event": "complete"},
    ]
    assert (_ADDRESS, True) in harness.changes


async def test_the_last_automatic_turn_says_so_and_the_limit_pauses_the_goal(
    harness: _Harness,
) -> None:
    await harness.command("Refactor the parser --max 2")
    await harness.settle()

    assert [content for content, _kind in harness.loop.started[1:]] == [
        GOAL_CONTINUE_TEXT,
        GOAL_LAST_TURN_TEXT,
    ]
    assert harness.goal()["status"] == "paused"
    assert harness.goal()["pause_reason"] == "limit"
    assert harness.goal()["objective"] == "Refactor the parser"
    assert harness.notes()[-1] == (
        "The goal was paused because it reached its limit of 2 automatic turns. "
        "The user can resume it with /goal resume."
    )
    assert harness.rows()[-1] == {"event": "paused", "reason": "limit", "max_turns": 2}


@pytest.mark.parametrize(
    ("turn", "reason", "note"),
    [
        ("fail", "run_failed", "your turn failed"),
        ("cancel", "stop", "the user stopped your turn"),
    ],
)
async def test_the_kernel_pauses_an_automatic_turn_that_cannot_go_on(
    harness: _Harness, turn: Any, reason: str, note: str
) -> None:
    async def fails(_run: Run) -> None:
        raise RuntimeError("provider down")

    async def cancelled(run: Run) -> None:
        harness.runs.cancel_by_session("coder", "session-one", project_id=None, reason="user")
        await asyncio.Event().wait()

    automatic = {"fail": fails, "cancel": cancelled}.get(turn, turn)
    harness.loop.turns = [_works, automatic]

    await harness.command("Fix the flaky test")
    await harness.settle()

    assert len(harness.loop.started) == 2
    assert harness.goal()["status"] == "paused"
    assert harness.goal()["pause_reason"] == reason
    assert harness.notes()[-1] == (
        f"The goal was paused because {note}. The user can resume it with /goal resume."
    )


async def test_a_user_reply_reactivates_a_blocked_goal_but_background_results_do_not(
    harness: _Harness,
) -> None:
    harness.loop.turns = [_marks(harness.goals, "blocked")]
    await harness.command("Deploy the service")
    await harness.settle()
    assert harness.goal()["status"] == "blocked"

    # A background result wakes the Session with an internal Run.
    harness.loop.turns = [_works]
    await harness.loop.start_run(
        "coder",
        "Background command finished.",
        session_id="session-one",
        internal=True,
        reply_surface=None,
        project_id=None,
        run_kind=RunKind.SYSTEM,
    )
    await harness.settle()
    assert harness.goal()["status"] == "blocked"
    assert len(harness.loop.started) == 2

    harness.loop.turns = [_marks(harness.goals, "complete")]
    await harness.user_run()
    await harness.settle()

    assert harness.loop.started[-1] == (GOAL_CONTINUE_TEXT, RunKind.SYSTEM)
    assert harness.goal()["status"] == "complete"
    assert harness.goal()["turns"] == 1
    assert [row["event"] for row in harness.rows()] == [
        "set",
        "blocked",
        "reactivated",
        "turn",
        "complete",
    ]


async def test_running_background_work_holds_the_next_automatic_turn(harness: _Harness) -> None:
    harness.work_pending = True

    await harness.command("Run the benchmark suite")
    await harness.settle()
    assert len(harness.loop.started) == 1
    assert harness.goal()["turns"] == 0

    # The work's result wakes the Session; that Run's end continues the goal.
    harness.work_pending = False
    harness.loop.turns = [_works, _marks(harness.goals, "complete")]
    await harness.loop.start_run(
        "coder",
        "Background command finished.",
        session_id="session-one",
        internal=True,
        reply_surface=None,
        project_id=None,
        run_kind=RunKind.SYSTEM,
    )
    await harness.settle()

    assert harness.loop.started[-1] == (GOAL_CONTINUE_TEXT, RunKind.SYSTEM)
    assert harness.goal()["status"] == "complete"


async def test_goal_commands_pause_resume_and_clear(harness: _Harness) -> None:
    harness.loop.turns = [_marks(harness.goals, "blocked")]
    await harness.command("Write the migration")
    await harness.settle()

    assert await harness.command("--max 0") == "Use --max with a number from 1 to 200."
    assert await harness.command("pause") == "Goal paused. Resume it with /goal resume."
    assert harness.goal()["pause_reason"] == "user"
    # The user paused it and knows; the Agent gets no pause note, only the row shows it.
    assert not any(note.startswith("The goal was paused") for note in harness.notes())
    assert harness.rows()[-1] == {"event": "paused", "reason": "user"}

    harness.loop.turns = [_marks(harness.goals, "complete")]
    await harness.command("resume")
    await harness.settle()
    assert harness.loop.started[-1] == ("/goal resume", RunKind.USER)
    resume_note = harness.loop.inputs[-1][1]
    assert resume_note is not None and resume_note.content == GOAL_RESUMED_TEXT
    assert harness.goal()["status"] == "complete"

    assert await harness.command("clear") == "Goal cleared."
    assert harness.rows()[-3:] == [
        {"event": "resumed"},
        {"event": "complete"},
        {"event": "cleared"},
    ]
    assert harness.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY) is None
    assert await harness.command(None) == (
        "This session has no goal. Set one with /goal <objective>."
    )


async def test_a_goal_is_not_set_while_a_run_is_active(harness: _Harness) -> None:
    release = asyncio.Event()

    async def waits(_run: Run) -> None:
        await release.wait()

    await harness.user_run(waits)

    assert await harness.command("Ship it") == "Stop the current turn first, then set the goal."
    assert harness.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY) is None
    release.set()
    await harness.settle()


@pytest.mark.parametrize("pause", ["stop", "takeover", "restart"])
async def test_stop_takeover_and_restart_pause_an_open_goal(harness: _Harness, pause: str) -> None:
    harness.loop.turns = [_marks(harness.goals, "blocked")]
    await harness.command("Translate the docs")
    await harness.settle()
    harness.sessions.mutate_metadata(
        _ADDRESS, lambda metadata: metadata[SESSION_GOAL_META_KEY].update(status="active")
    )

    if pause == "stop":
        assert await harness.goals.pause_for_stop(_ADDRESS)
    elif pause == "takeover":
        assert await harness.goals.pause_for_takeover(_ADDRESS)
    else:
        assert harness.goals.pause_after_restart() == 1

    assert harness.goal()["status"] == "paused"
    assert harness.goal()["pause_reason"] == pause
    assert harness.notes()[-1].startswith("The goal was paused because ")
    assert await harness.goals.pause_for_stop(_ADDRESS) is False
