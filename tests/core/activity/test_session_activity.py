"""Session activity: working, waiting and idle across Runs, commands, results and Sub-Agents.

Runs come from a real ``ChatRunManager``; commands, held results and Sub-Agent
links are fakes the test sets. Time is a controlled clock: the idle settle
period passes only when the test lets it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core.activity import (
    IDLE,
    IDLE_SETTLE_SECONDS,
    FinishedWork,
    SessionActivity,
    SessionActivityState,
    WaitingFor,
)
from core.runs import ChatRunManager, Run, RunAdmission
from core.sessions import SessionAddress
from core.subagents.links import SubAgentLink
from core.tools.command_manager import CommandNotFoundError

pytestmark = pytest.mark.asyncio

PARENT = SessionAddress("project", "lead", "parent")
CHILD = SessionAddress("project", "worker", "child")
GRANDCHILD = SessionAddress("project", "helper", "grandchild")
DIRECT = SessionAddress("project", "worker", "talks-to-user")
START = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)

Callbacks = list[Callable[..., None]]


def _subscribe(callbacks: Callbacks, callback: Callable[..., None]) -> Callable[[], None]:
    callbacks.append(callback)
    return lambda: callbacks.remove(callback)


@dataclass
class _Clock:
    """Wall clock, monotonic time and the settle sleep, all under the test's control."""

    seconds: float = 0.0
    sleepers: list[asyncio.Future[None]] = field(default_factory=list)

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    async def sleep(self, delay: float) -> None:
        assert delay == IDLE_SETTLE_SECONDS
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append(future)
        await future

    async def settle(self) -> None:
        """Let the settle period of every Session waiting for it pass."""
        self.seconds += IDLE_SETTLE_SECONDS
        sleepers, self.sleepers = self.sleepers, []
        for sleeper in sleepers:
            if not sleeper.done():
                sleeper.set_result(None)
        await _turns()


@dataclass
class _Commands:
    pending: dict[SessionAddress, int] = field(default_factory=dict)
    owners: dict[str, SessionAddress] = field(default_factory=dict)
    callbacks: Callbacks = field(default_factory=list)

    def add_changed_callback(self, callback: Callable[[str], None]) -> Callable[[], None]:
        return _subscribe(self.callbacks, callback)

    def pending_wakes(self, owner: SessionAddress) -> int:
        return self.pending.get(owner, 0)

    def report(self, command_id: str) -> SimpleNamespace:
        if command_id not in self.owners:
            raise CommandNotFoundError(command_id)
        return SimpleNamespace(owner=self.owners[command_id])

    def set(self, command_id: str, owner: SessionAddress, pending: int) -> None:
        self.owners[command_id] = owner
        self.pending[owner] = pending
        for callback in list(self.callbacks):
            callback(command_id)


@dataclass
class _Completions:
    pending: dict[SessionAddress, int] = field(default_factory=dict)
    callbacks: Callbacks = field(default_factory=list)

    def add_completion_changed_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]:
        return _subscribe(self.callbacks, callback)

    def pending_completion_wakes(self, address: SessionAddress) -> int:
        return self.pending.get(address, 0)

    def set(self, address: SessionAddress, pending: int) -> None:
        self.pending[address] = pending
        for callback in list(self.callbacks):
            callback(address)


@dataclass
class _SubAgents:
    links: dict[SessionAddress, SubAgentLink] = field(default_factory=dict)
    forwarding: set[SessionAddress] = field(default_factory=set)
    callbacks: Callbacks = field(default_factory=list)

    def link(self, child: SessionAddress, parent: SessionAddress, reply_target: str) -> None:
        self.links[child] = SubAgentLink(child.session_id, child, parent, "", reply_target)

    def add_forwarded_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]:
        return _subscribe(self.callbacks, callback)

    def forwarding_pending(self, address: SessionAddress) -> bool:
        return address in self.forwarding

    async def parent_link(self, address: SessionAddress) -> SubAgentLink | None:
        return self.links.get(address)

    def forwarded(self, address: SessionAddress) -> None:
        self.forwarding.discard(address)
        for callback in list(self.callbacks):
            callback(address)


@dataclass
class _World:
    runs: ChatRunManager
    commands: _Commands
    completions: _Completions
    subagents: _SubAgents
    clock: _Clock
    activity: SessionActivity
    changes: list[tuple[SessionAddress, SessionActivityState, FinishedWork | None]]
    gates: dict[str, asyncio.Event] = field(default_factory=dict)

    async def start(self, address: SessionAddress, *, contributes: bool = True) -> Run:
        gate = asyncio.Event()

        async def execute(_run: Run) -> str:
            await gate.wait()
            return "done"

        run = await self.runs.start(
            address, execute, admission=RunAdmission(contributes_to_agent_activity=contributes)
        )
        self.gates[run.id] = gate
        await _turns()
        return run

    async def finish(self, run: Run) -> None:
        self.gates[run.id].set()
        await run.wait()
        await _turns()

    def states(self, address: SessionAddress) -> list[str]:
        return [state.state for changed, state, _work in self.changes if changed == address]


async def _turns(count: int = 30) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    runs = ChatRunManager()
    commands, completions, subagents, clock = _Commands(), _Completions(), _SubAgents(), _Clock()
    activity = SessionActivity(
        runs=runs,
        commands=commands,
        completions=completions,
        subagents=subagents,
        clock=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    changes: list[tuple[SessionAddress, SessionActivityState, FinishedWork | None]] = []
    activity.add_changed_callback(lambda *change: changes.append(change))
    world = _World(runs, commands, completions, subagents, clock, activity, changes)
    try:
        yield world
    finally:
        await activity.aclose()
        await runs.aclose()


async def test_a_session_waits_for_its_commands_and_results_and_ends_one_span(
    world: _World,
) -> None:
    run = await world.start(PARENT)
    assert world.activity.state(PARENT) == SessionActivityState("working", START)
    world.commands.set("cmd", PARENT, 1)
    world.clock.seconds = 30
    await world.finish(run)

    # The command will wake the Session: waiting, in the same span.
    waiting = SessionActivityState("waiting", START, WaitingFor(commands=1))
    assert world.activity.state(PARENT) == waiting
    assert world.activity.snapshot() == [(PARENT, waiting)]
    # Its result reaches completion delivery, which holds it until it wakes the Session.
    world.completions.set(PARENT, 1)
    world.commands.set("cmd", PARENT, 0)
    await _turns()
    assert world.activity.state(PARENT).waiting_for == WaitingFor(results=1)

    world.clock.seconds = 45
    world.completions.set(PARENT, 0)
    await _turns()
    world.clock.seconds = 46
    # Idle only after the settle period, confirmed by a recheck.
    assert world.activity.state(PARENT).state == "waiting"
    await world.clock.settle()

    assert world.activity.state(PARENT) == IDLE
    assert world.activity.snapshot() == []
    assert world.states(PARENT) == ["working", "waiting", "waiting", "idle"]
    # The span ends when the Session became idle, not when that was confirmed.
    assert world.changes[-1][2] == FinishedWork(
        started_at=START, finished_at=START + timedelta(seconds=45), duration_ms=45_000
    )
    assert all(work is None for _address, _state, work in world.changes[:-1])


@pytest.mark.parametrize("during_recheck", [False, True])
async def test_a_run_inside_the_settle_period_continues_the_span(
    world: _World, monkeypatch: pytest.MonkeyPatch, during_recheck: bool
) -> None:
    if during_recheck:
        world.subagents.link(CHILD, PARENT, "user")
        await world.start(CHILD)
    first = await world.start(PARENT)
    world.clock.seconds = 10
    await world.finish(first)
    # Idle, but not yet confirmed: nothing is published.
    assert world.states(PARENT) == ["working"]

    release = asyncio.Event()
    if during_recheck:
        entered = asyncio.Event()
        parent_link = world.subagents.parent_link

        async def blocked_link(address: SessionAddress) -> SubAgentLink | None:
            if address == CHILD:
                entered.set()
                await release.wait()
            return await parent_link(address)

        monkeypatch.setattr(world.subagents, "parent_link", blocked_link)
        await world.clock.settle()
        await entered.wait()

    world.clock.seconds += 1
    # A Run excluded from Agent activity is work all the same.
    second = await world.start(PARENT, contributes=False)
    release.set()
    await world.clock.settle()
    assert world.activity.state(PARENT) == SessionActivityState(
        "working", START, contributes_to_agent_activity=False
    )
    assert all(work is None for address, _state, work in world.changes if address == PARENT)
    world.clock.seconds = 20
    await world.finish(second)
    await world.clock.settle()

    assert world.states(PARENT) == ["working", "working", "idle"]
    assert world.changes[-1][2] == FinishedWork(START, START + timedelta(seconds=20), 20_000)


async def test_a_parent_waits_for_sub_agents_answering_it_until_their_answers_arrive(
    world: _World,
) -> None:
    world.subagents.link(CHILD, PARENT, "parent")
    world.subagents.link(GRANDCHILD, CHILD, "parent")
    world.subagents.link(DIRECT, PARENT, "user")
    parent_run = await world.start(PARENT)
    grandchild_run = await world.start(GRANDCHILD)
    direct_run = await world.start(DIRECT)
    await world.finish(parent_run)

    # The grandchild works for the child, which waits for it and so keeps the
    # Parent waiting; a Sub-Agent answering the user does not count.
    assert world.activity.state(CHILD).waiting_for == WaitingFor(subagents=1)
    assert world.activity.state(PARENT).waiting_for == WaitingFor(subagents=1)
    await world.finish(direct_run)
    await world.clock.settle()
    assert world.activity.state(DIRECT) == IDLE

    # The grandchild's Run ended, but its answer is not handed over yet.
    world.subagents.forwarding.add(GRANDCHILD)
    await world.finish(grandchild_run)
    await world.clock.settle()
    assert world.activity.state(GRANDCHILD) == IDLE
    assert world.activity.state(CHILD).waiting_for == WaitingFor(subagents=1)
    # The answer reaches the child's completion delivery.
    world.completions.set(CHILD, 1)
    world.subagents.forwarded(GRANDCHILD)
    await _turns()
    assert world.activity.state(CHILD).waiting_for == WaitingFor(results=1)
    assert world.activity.state(PARENT).state == "waiting"

    world.completions.set(CHILD, 0)
    await _turns()
    await world.clock.settle()
    assert world.activity.state(CHILD) == IDLE
    # The Parent settles only after the child it waited for.
    assert world.activity.state(PARENT).state == "waiting"
    await world.clock.settle()
    assert world.activity.state(PARENT) == IDLE
    assert world.states(PARENT)[-1] == "idle"
