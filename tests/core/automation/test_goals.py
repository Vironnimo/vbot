"""Contract tests for Session goals: ``/goal``, automatic Runs and their pauses."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from core.automation.goals import (
    GOAL_CONTINUE_TEXT,
    GOAL_LAST_TURN_TEXT,
    GOAL_RESUMED_TEXT,
    GoalService,
)
from core.chat import ChatSessionManager, ReplySurface
from core.chat.commands import CommandExecutionContext
from core.runs import ChatRunManager, Run, RunAdmission, RunKind
from core.sessions import SESSION_GOAL_META_KEY, SessionAddress

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("current_format_data_directory")]

_ADDRESS = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
Turn = Callable[[Run], Awaitable[None]]


async def _works(run: Run) -> None:
    run.succeeded_tool_call_names.add("bash")


async def _talks(_run: Run) -> None:
    """A turn that only answers, without a successful Tool call."""


class _GoalChatLoop:
    """Start each Run with the next scripted turn; unscripted turns do work."""

    def __init__(self, manager: ChatRunManager) -> None:
        self._manager = manager
        self.turns: list[Turn] = []
        self.started: list[tuple[str, RunKind]] = []

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
    ) -> Run:
        turn = self.turns.pop(0) if self.turns else _works

        async def executor(run: Run) -> str:
            run.internal = internal
            self.started.append((content, run_kind))
            await turn(run)
            return "done"

        return await self._manager.start(
            SessionAddress(project_id, agent_id, session_id),
            executor,
            admission=RunAdmission(run_kind=run_kind),
        )


class _Harness:
    def __init__(self, tmp_path: Path, *, work_pending: bool = False) -> None:
        self.runs = ChatRunManager()
        self.sessions = ChatSessionManager(tmp_path)
        self.sessions.create("coder", session_id="session-one")
        self.loop = _GoalChatLoop(self.runs)
        self.work_pending = work_pending
        self.changes: list[SessionAddress] = []

        async def pending(_address: SessionAddress) -> bool:
            return self.work_pending

        self.goals = GoalService(
            sessions=self.sessions,
            run_manager=self.runs,
            chat_loop=cast(Any, self.loop),
            work_pending=pending,
        )
        self.goals.install()
        self.goals.add_changed_callback(self.changes.append)

    async def command(self, argument: str | None) -> str:
        outcome = await self.goals.execute_command(
            CommandExecutionContext(
                agent_id="coder",
                session_id="session-one",
                project_id=None,
                reply_surface=ReplySurface.webui(),
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
            if message.role == "note"
        ]

    async def settle(self) -> None:
        """Wait until no Run is active and every goal decision has run."""
        for _ in range(2000):
            await asyncio.sleep(0)
            if not self.runs.active_runs() and not self.goals._tasks:  # noqa: SLF001
                await asyncio.sleep(0)
                if not self.runs.active_runs() and not self.goals._tasks:  # noqa: SLF001
                    return
        raise AssertionError("goal Runs did not settle")

    async def user_run(self, turn: Turn = _works) -> None:
        """A message the user sends: a visible user Run."""

        async def executor(run: Run) -> str:
            await turn(run)
            return "answer"

        await self.runs.start(_ADDRESS, executor)


def _marks(goals: GoalService, status: Any) -> Turn:
    async def turn(run: Run) -> None:
        run.succeeded_tool_call_names.add("bash")
        await goals.update_from_agent(_ADDRESS, status, run_id=run.id)

    return turn


async def test_goal_runs_automatically_until_the_agent_marks_it_complete(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.loop.turns = [_works, _works, _marks(harness.goals, "complete")]

    await harness.command("Ship <the> release")
    await harness.settle()

    first, *automatic = harness.loop.started
    # The user's command starts the first Run; the objective stays quoted user text.
    assert first[1] is RunKind.USER
    assert first[0].startswith("The user set a goal for this session with /goal:\n")
    assert '"Ship \\u003cthe\\u003e release"' in first[0]
    assert automatic == [(GOAL_CONTINUE_TEXT, RunKind.SYSTEM)] * 2
    assert harness.goal()["status"] == "complete"
    assert harness.goal()["turns"] == 2
    assert await harness.command(None) == (
        "Goal (complete): Ship <the> release\nAutomatic turns: 2 of 30."
    )
    assert harness.changes


async def test_the_last_automatic_turn_says_so_and_the_limit_pauses_the_goal(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)

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


@pytest.mark.parametrize(
    ("turn", "reason", "note"),
    [
        (_talks, "no_progress", "an automatic turn ended without a successful tool call"),
        ("fail", "run_failed", "your turn failed"),
        ("cancel", "stop", "the user stopped your turn"),
    ],
)
async def test_the_kernel_pauses_an_automatic_turn_that_cannot_go_on(
    tmp_path: Path, turn: Any, reason: str, note: str
) -> None:
    harness = _Harness(tmp_path)

    async def fails(_run: Run) -> None:
        raise RuntimeError("provider down")

    async def cancelled(run: Run) -> None:
        harness.runs.cancel_by_session("coder", "session-one", project_id=None, reason="user")
        await asyncio.sleep(1)

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
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
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


async def test_running_background_work_holds_the_next_automatic_turn(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, work_pending=True)

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


async def test_goal_commands_pause_resume_and_clear(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.loop.turns = [_marks(harness.goals, "blocked")]
    await harness.command("Write the migration")
    await harness.settle()

    assert await harness.command("--max 0") == "Use --max with a number from 1 to 200."
    assert await harness.command("pause") == "Goal paused. Resume it with /goal resume."
    assert harness.goal()["pause_reason"] == "user"
    # The user paused it and knows; the Agent gets no pause note.
    assert not any(note.startswith("The goal was paused") for note in harness.notes())

    harness.loop.turns = [_marks(harness.goals, "complete")]
    await harness.command("resume")
    await harness.settle()
    assert harness.loop.started[-1] == (GOAL_RESUMED_TEXT, RunKind.USER)
    assert harness.goal()["status"] == "complete"

    assert await harness.command("clear") == "Goal cleared."
    assert harness.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY) is None
    assert await harness.command(None) == (
        "This session has no goal. Set one with /goal <objective>."
    )


async def test_a_goal_is_not_set_while_a_run_is_active(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    release = asyncio.Event()

    async def waits(_run: Run) -> None:
        await release.wait()

    await harness.user_run(waits)

    assert await harness.command("Ship it") == "Stop the current turn first, then set the goal."
    assert harness.sessions.metadata_value(_ADDRESS, SESSION_GOAL_META_KEY) is None
    release.set()
    await harness.settle()


@pytest.mark.parametrize("pause", ["stop", "takeover", "restart"])
async def test_stop_takeover_and_restart_pause_an_open_goal(tmp_path: Path, pause: str) -> None:
    harness = _Harness(tmp_path)
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
