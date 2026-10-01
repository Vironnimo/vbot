"""What the Live operator keeps from one call to the next, and when it forgets."""

from __future__ import annotations

import pytest

from server.live._memory import LiveMemory
from tests.server.live.tools_test_support import Fixture, session_row

HOUR = 3600.0


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_a_later_call_keeps_the_refs_and_hears_the_assignments_of_earlier_calls() -> None:
    clock = Clock()
    memory = LiveMemory(clock=clock)
    assert memory.begin_call() == ""

    first = Fixture(memory)
    first.app.sessions = [session_row("ses_run", "coder", title="Fix login", has_active_run=True)]
    await first.ok("overview")
    await first.ok("start_agent_session", agent="coder", task="Write the\nrelease notes")
    await first.ok("open", target="s2")
    await first.ok("read", target="s1")
    first.app.add_terminal("term_a", name="Build")
    await first.ok("terminal", action="maximize", target="term_a")
    clock.now += 25 * 60

    recap = memory.begin_call()
    # Only assignments are told back; looking at and arranging the app are not.
    assert recap.splitlines() == [
        "What you did, oldest first:",
        '- 25 min ago: start_agent_session {"agent": "coder", "task": "Write the release '
        'notes"} -> Started a Session at Coder with the task: s2. It works in the background; '
        "an update follows when it finishes.",
        "Refs:",
        "- s2: Session at Coder",
        '- s1: Session at Coder "Fix login"',
        '- t1: Codex Terminal "Build"',
    ]
    second = Fixture(memory)
    assert second.executor.session_ref("coder", "ses_new1") == "s2"
    assert second.executor.session_ref("writer", "ses_other") == "s3"


@pytest.mark.asyncio
async def test_long_values_are_shortened_and_only_the_latest_assignments_are_kept() -> None:
    memory = LiveMemory(clock=Clock())
    fx = Fixture(memory)
    for index in range(10):
        await fx.ok("start_agent_session", agent="coder", task=f"Task {index} " + "x" * 300)

    notes = memory.begin_call().split("\nRefs:\n")[0].splitlines()[1:]
    assert len(notes) == 8
    assert notes[0].startswith(
        '- under a minute ago: start_agent_session {"agent": "coder", "task": "Task 2 xxx'
    )
    assert all('xxx..."}' in note and len(note) < 400 for note in notes)


@pytest.mark.parametrize(
    ("idle_hours", "kept"), [(7.9, True), (8.1, False)], ids=["within-8h", "after-8h"]
)
@pytest.mark.asyncio
async def test_the_memory_is_forgotten_after_eight_hours_without_use(
    idle_hours: float, kept: bool
) -> None:
    clock = Clock()
    memory = LiveMemory(clock=clock)
    memory.begin_call()
    await Fixture(memory).ok("start_agent_session", agent="coder", task="Fix it")
    # Using the memory restarts the idle period, so the earlier hours do not count.
    clock.now += 5 * HOUR
    memory.touch()
    clock.now += idle_hours * HOUR

    recap = memory.begin_call()
    assert ("start_agent_session" in recap) is kept
    assert Fixture(memory).executor.session_ref("writer", "ses_other") == ("s2" if kept else "s1")
