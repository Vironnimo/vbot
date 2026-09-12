"""Terminal manager: resize behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.tools._terminal_state as terminal_state
from core.tools.terminal_manager import (
    TerminalManager,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_manager_helpers import (
    TEST_ACTIVITY_QUIET_SECONDS,
    AdapterFactory,
    FakeClock,
    PendingTriggerService,
    establish_delivered_baseline,
    eventually,
    owner,
    settle_next_activity,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import (
    terminal_manager as terminal_manager,
)


@pytest.mark.asyncio
async def test_screen_revision_guards_input_and_resize_updates_both_sides(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("PROMPT> ")
    await eventually(lambda: session.renderer.revision > 0)

    with pytest.raises(TerminalStaleScreenError):
        await manager.send_input(
            session.terminal_id,
            owner(),
            text="answer",
            key="enter",
            expected_screen_revision=0,
            origin_run_id="run-b",
        )

    revision = session.renderer.revision
    await manager.send_input(
        session.terminal_id,
        owner(),
        text="answer",
        key="enter",
        expected_screen_revision=revision,
        origin_run_id="run-b",
    )
    assert adapter.writes == ["answer", "\r"]

    result = await manager.resize(session.terminal_id, owner(), columns=100, rows=24)
    assert adapter.resizes == [(24, 100)]
    assert result["columns"] == 100
    assert result["rows"] == 24


@pytest.mark.asyncio
async def test_resize_to_current_dimensions_is_a_no_op(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)

    result = await manager.resize(session.terminal_id, owner(), columns=120, rows=32)
    operator_result = await manager.resize_for_operator(session.terminal_id, columns=120, rows=32)

    assert factory.adapters[0].resizes == []
    assert result["columns"] == 120
    assert result["rows"] == 32
    assert result["screen_revision"] == session.renderer.revision
    assert operator_result["screen_revision"] == session.renderer.revision
    assert session.attention is None
    assert session.state != "working"


@pytest.mark.asyncio
async def test_operator_resize_burst_without_output_does_not_wake_agent(tmp_path: Path) -> None:
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        revision = session.attention_revision
        for columns, rows in [(70, 20), (160, 48), (100, 30)]:
            await manager.resize_for_operator(session.terminal_id, columns=columns, rows=rows)
        await clock.advance(terminal_state.TERMINAL_RESIZE_GRACE_MAX_SECONDS + 1)
        assert len(trigger.submissions) == 1
        assert session.attention_revision == revision
        assert session.state == "ready"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_resize_hard_deadline_caps_repaint_suppression(tmp_path: Path) -> None:
    """A repaint stream must not suppress Agent delivery past the hard cap."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await manager.resize(session.terminal_id, owner(), columns=90, rows=24)
        generation = session.activity_generation
        factory.adapters[0].emit("repaint wave 1")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1

        await clock.advance(terminal_state.TERMINAL_RESIZE_GRACE_MAX_SECONDS)
        await eventually(lambda: len(trigger.submissions) == 2)
        generation = session.activity_generation
        factory.adapters[0].emit("work after grace cap")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 3)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_agent_input_clears_resize_grace_and_wakes_on_later_output(
    tmp_path: Path,
) -> None:
    """Input after a resize is work, so it ends the grace: a settle following
    it must deliver immediately instead of being treated as repaint noise."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await manager.resize(session.terminal_id, owner(), columns=100, rows=24)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data="answer\r",
            text=None,
            key=None,
            expected_screen_revision=None,
            origin_run_id="run-c",
        )
        assert session.resize_grace_deadline == 0.0
        generation = session.activity_generation
        factory.adapters[0].emit("output after agent input")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 2)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unchanged_screen_stays_silent_until_content_changes(
    tmp_path: Path,
) -> None:
    """Suppression only covers an unchanged screen: once the screen actually
    changes, the next quiet boundary wakes the agent again."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> ")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1

        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> \nsecond option")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 2)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_textless_agent_start_suppresses_the_startup_settle(
    tmp_path: Path,
) -> None:
    """A text-less Agent start stays silent until the first explicit input:
    the startup screen (banner, prompt, TUI boot) is observed by the starting
    Agent and must not wake the session."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        session.state = "working"

        generation = session.activity_generation
        factory.adapters[0].emit("TUI banner")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert trigger.submissions == []
        assert session.attention is None or not session.attention.delivered

        generation = session.activity_generation
        factory.adapters[0].emit("\rTUI banner")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert trigger.submissions == []

        await manager.send_input(
            session.terminal_id,
            owner(),
            data="go\r",
            text=None,
            key=None,
            expected_screen_revision=None,
            origin_run_id="run-0",
        )
        generation = session.activity_generation
        factory.adapters[0].emit("output after input")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 1)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_repeated_resize_output_is_deferred_without_losing_final_content(
    tmp_path: Path,
) -> None:
    """Repeated final output during resize must remain eligible for delivery."""
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )

        await manager.resize(session.terminal_id, owner(), columns=90, rows=24)
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> ")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> status")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> status")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1

        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> status")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1

        await clock.advance(terminal_state.TERMINAL_RESIZE_GRACE_MAX_SECONDS)
        await eventually(lambda: len(trigger.submissions) == 2)
        generation = session.activity_generation
        factory.adapters[0].emit("\rMENU> status\nnew work output line")
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await eventually(lambda: len(trigger.submissions) == 3)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledge", [False, True])
@pytest.mark.parametrize(
    "output",
    ["\r\x1b[2KTask completed. Please review result.", "\r\x1b[7mMENU> \x1b[0m"],
)
async def test_resize_final_output_is_delivered_without_another_pty_event(
    tmp_path: Path, acknowledge: bool, output: str
) -> None:
    clock = FakeClock()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    manager.start()
    try:
        session = await establish_delivered_baseline(
            manager,
            factory,
            trigger,
            clock,
            tmp_path,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        await manager.resize(session.terminal_id, owner(), columns=90, rows=24)
        generation = session.activity_generation
        factory.adapters[0].emit(output)
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == 1
        if acknowledge:
            manager.acknowledge_attention(session.terminal_id, owner(), session.attention_revision)
        await clock.advance(terminal_state.TERMINAL_RESIZE_GRACE_MAX_SECONDS)
        await eventually(lambda: session.settle_task is not None and session.settle_task.done())
        if not acknowledge:
            await eventually(lambda: len(trigger.submissions) == 2)
        assert len(trigger.submissions) == (1 if acknowledge else 2)
        generation = session.activity_generation
        factory.adapters[0].emit(output)
        await settle_next_activity(
            clock,
            session,
            after_generation=generation,
            quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        )
        assert len(trigger.submissions) == (1 if acknowledge else 2)
    finally:
        await manager.aclose()
