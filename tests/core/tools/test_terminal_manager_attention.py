"""Terminal manager: attention delivery to the attached Agent Session, and resize grace."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

import core.tools._terminal_state as terminal_state
import core.tools.process_manager as process_manager
import core.tools.terminal_manager as terminal_module
from core.runs import RunAdmissionBlockedError
from core.tools.terminal_manager import (
    TerminalClosedError,
    TerminalManager,
    TerminalNotOwnedError,
    TerminalOwner,
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
    session_of,
    settle_next_activity,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import clocked_manager as clocked_manager
from tests.core.tools.terminal_manager_helpers import delivering_manager as delivering_manager
from tests.core.tools.terminal_manager_helpers import shell_environment as shell_environment
from tests.core.tools.terminal_manager_helpers import terminal_manager as terminal_manager

Delivering = tuple[TerminalManager, AdapterFactory, PendingTriggerService]
Clocked = tuple[TerminalManager, AdapterFactory, PendingTriggerService, FakeClock]


@pytest.fixture
def host_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])


async def _agent_input(manager: TerminalManager, session: Any, **fields: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "data": None,
        "text": None,
        "key": None,
        "expected_screen_revision": None,
        "origin_run_id": "run-b",
    }
    return await manager.send_input(session.terminal_id, owner(), **{**arguments, **fields})


@pytest.mark.asyncio
async def test_operator_activity_wakes_the_attached_agent_session(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    session = await spawn(manager, tmp_path)
    # A text-less Agent start suppresses the first settle (the startup screen
    # is not work); operator input re-arms delivery.
    await manager.send_operator_input(session.terminal_id, "look\r")
    factory.adapters[0].emit("screen changed")
    await eventually(lambda: session.attention_revision == 1)

    assert session.state == "ready"
    assert session.attention is not None
    assert session.attention.kind == "output_settled"
    await eventually(lambda: len(trigger.submissions) == 1)
    assert trigger.submissions[0][0] == ("agent-a", "session-a")


@pytest.mark.asyncio
async def test_wait_exit_and_explicit_kill_have_distinct_attention(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    natural = await spawn(manager, tmp_path)
    factory.adapters[0].finish(7)
    await eventually(lambda: natural.state == "exited")
    snapshot, timed_out = await manager.wait_for_attention(
        natural.terminal_id, owner(), after_revision=0, timeout_ms=10
    )
    assert not timed_out
    assert snapshot["attention"]["kind"] == "exited"
    assert snapshot["exit_code"] == 7

    killed = await spawn(manager, tmp_path)
    result = await manager.kill(killed.terminal_id, owner())
    assert result["state"] == "exited"
    assert result["attention"] is None


@pytest.mark.asyncio
async def test_attention_auto_delivers_and_manual_ack_cancels_exactly_once(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    session = await spawn(manager, tmp_path)
    await _agent_input(manager, session, text="do work", key="enter")
    factory.adapters[0].emit("working...\r\nREADY> ")
    await eventually(lambda: len(trigger.submissions) == 1)

    args, kwargs = trigger.submissions[0]
    assert args == ("agent-a", "session-a")
    assert kwargs["origin_run_id"] == "run-b"
    assert kwargs["project_id"] == "project-a"
    assert isinstance(kwargs["body"], str)
    assert session.terminal_id in kwargs["body"]
    assert "```" in kwargs["body"]
    assert "working..." in kwargs["body"]
    assert session.attention is not None
    assert session.attention.kind == "output_settled"

    manager.acknowledge_attention(session.terminal_id, owner(), 1)
    await asyncio.sleep(0)
    assert len(trigger.cancellations) == 1
    assert session.acknowledged_attention_revision == 1
    assert session.notification_task is not None
    assert session.notification_task.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_closed", [True, False], ids=["owner-closed", "fault"])
async def test_failed_attention_delivery_stays_undelivered_and_logs_only_faults(
    delivering_manager: Delivering,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_closed: bool,
) -> None:
    manager, factory, trigger = delivering_manager
    trigger.error = (
        RunAdmissionBlockedError("completion owner can no longer receive work")
        if owner_closed
        else RuntimeError("delivery broke")
    )
    errors: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        process_manager._LOGGER, "error", lambda *args, **_kwargs: errors.append(args)
    )
    session = await spawn(manager, tmp_path)
    await _agent_input(manager, session, text="do work", key="enter")
    factory.adapters[0].emit("working...\r\nREADY> ")
    await eventually(lambda: len(trigger.submissions) == 1)
    task = session.notification_task
    assert task is not None
    await asyncio.wait({task})

    assert session.attention is not None
    assert session.attention.delivered is False
    # A closed execution owner is an expected lifecycle end, not an error.
    assert len(errors) == (0 if owner_closed else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["late output", "\x1b[1;1H"])
async def test_new_output_postpones_a_pending_agent_wakeup_to_the_next_quiet_boundary(
    delivering_manager: Delivering, tmp_path: Path, output: str
) -> None:
    manager, factory, trigger = delivering_manager
    session = await spawn(manager, tmp_path)
    await _agent_input(manager, session, data="begin")
    await eventually(lambda: len(trigger.submissions) == 1)

    factory.adapters[0].emit(output)
    await eventually(lambda: len(trigger.cancellations) == 1)
    await eventually(lambda: len(trigger.submissions) == 2)

    assert session.attention_revision == 2
    assert session.attention is not None
    assert session.attention.kind == "output_settled"
    assert trigger.submissions[0][1]["notice_id"] != trigger.submissions[1][1]["notice_id"]
    assert trigger.submissions[1][1]["origin_run_id"] == "run-b"


@pytest.mark.asyncio
async def test_session_move_reroutes_pending_attention_to_new_owner(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, _factory, trigger = delivering_manager
    session = await spawn(manager, tmp_path)
    await _agent_input(manager, session, text="do work", key="enter")
    await eventually(lambda: len(trigger.submissions) == 1)
    target = TerminalOwner("project-b", "agent-b", "session-a")

    assert manager.transfer_scope(owner(), target) == 1
    await eventually(lambda: len(trigger.submissions) == 2)

    assert trigger.submissions[0][0] == ("agent-a", "session-a")
    assert trigger.submissions[1][0] == ("agent-b", "session-a")
    assert trigger.submissions[1][1]["project_id"] == "project-b"
    assert len(trigger.cancellations) == 1
    assert manager.get_session(session.terminal_id, target) is session


@pytest.mark.asyncio
async def test_notification_revision_authorizes_only_the_delivered_screen(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    session = await spawn(manager, tmp_path)
    await manager.send_operator_input(session.terminal_id, "open")
    factory.adapters[0].emit("Approve fixture change? [y/n]")
    await eventually(lambda: len(trigger.submissions) == 1)
    body = trigger.submissions[0][1]["body"]
    match = re.search(r"^screen_revision: (\d+)$", body, re.MULTILINE)
    assert match is not None
    revision = int(match[1])
    assert revision == session.renderer.revision
    assert "Approve fixture change? [y/n]" in body

    sent = await _agent_input(
        manager, session, text="y", key="enter", expected_screen_revision=revision
    )
    assert sent["characters_sent"] == 2
    assert factory.adapters[0].writes[-2:] == ["y", "\r"]
    writes = list(factory.adapters[0].writes)
    # The answer changed the screen revision before any echo arrived, so a replay is refused.
    with pytest.raises(TerminalStaleScreenError):
        await _agent_input(
            manager, session, text="y", key="enter", expected_screen_revision=revision
        )
    assert factory.adapters[0].writes == writes


@pytest.mark.asyncio
async def test_closed_pty_write_marks_terminal_exited_and_delivers_attention(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    session = await spawn(manager, tmp_path)
    manager.attach(session.terminal_id, owner(), origin_run_id="attach-run")
    factory.adapters[0].write_error = EOFError("Pty is closed")

    with pytest.raises(TerminalClosedError):
        await manager.send_operator_input(session.terminal_id, "late input")

    await eventually(lambda: len(trigger.submissions) == 1)
    assert session.state == "exited"
    assert session.attention is not None
    assert session.attention.kind == "exited"
    assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"


@pytest.mark.asyncio
@pytest.mark.usefixtures("host_shell")
async def test_unattached_operator_terminal_has_no_agent_scope_or_attention_delivery(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    result = await manager.spawn_for_operator(command=None, arguments=["--login"], cwd=tmp_path)
    terminal_id = result["terminal_id"]
    session = session_of(manager, terminal_id)

    assert factory.calls[0][0] == ["host-shell", "--login"]
    assert (result["owner"], result["attachment"]) == (None, None)
    assert (session.owner, session.lifecycle_owner, session.attachment) == (None, None, None)

    await manager.close_project_scope("project-a")
    assert factory.adapters[0].alive is True

    await manager.send_operator_input(terminal_id, "echo ready\r")
    factory.adapters[0].emit("ready\r\n")
    await eventually(lambda: session.state == "ready")
    factory.adapters[0].finish(0)
    await eventually(lambda: session.state == "exited")
    assert trigger.submissions == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("host_shell")
async def test_operator_terminal_attach_delivers_activity_and_detach_preserves_lifetime(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    result = await manager.spawn_for_operator(
        command=None, arguments=[], cwd=tmp_path, columns=137, rows=41
    )
    terminal_id = result["terminal_id"]
    session = session_of(manager, terminal_id)

    attached, changed = manager.attach(terminal_id, owner(), origin_run_id="attach-run")
    assert (attached, changed) == (session, True)
    assert (session.owner, session.lifecycle_owner, session.attachment) == (None, None, owner())
    assert manager.get_session(terminal_id, owner()) is session
    same, changed = manager.attach(terminal_id, owner(), origin_run_id="attach-run-2")
    assert (same, changed) == (session, False)
    # Attaching adopts the operator's size instead of resizing the program.
    assert (session.renderer.columns, session.renderer.rows) == (137, 41)
    assert factory.adapters[0].resizes == []

    await manager.send_operator_input(terminal_id, "echo ready\r")
    factory.adapters[0].emit("ready\r\n")
    await eventually(lambda: len(trigger.submissions) == 1)
    assert trigger.submissions[0][0] == ("agent-a", "session-a")
    assert trigger.submissions[0][1]["origin_run_id"] == "attach-run-2"

    assert manager.detach(terminal_id, owner()) is session
    assert session.attachment is None
    with pytest.raises(TerminalNotOwnedError):
        manager.get_session(terminal_id, owner())
    await manager.close_scope(owner())
    assert factory.adapters[0].alive is True


@pytest.mark.asyncio
async def test_attach_arms_an_already_working_terminal_for_its_next_quiet_boundary(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, _factory, trigger = delivering_manager
    result = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
    session = session_of(manager, result["terminal_id"])
    session.state = "working"

    manager.attach(session.terminal_id, owner(), origin_run_id="attach-run")

    await eventually(lambda: len(trigger.submissions) == 1)
    assert session.attention is not None
    assert session.attention.kind == "output_settled"
    assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"


@pytest.mark.asyncio
async def test_resize_to_current_dimensions_is_a_no_op(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)

    result = await manager.resize(session.terminal_id, owner(), columns=120, rows=32)
    operator_result = await manager.resize_for_operator(session.terminal_id, columns=120, rows=32)

    assert factory.adapters[0].resizes == []
    assert (result["columns"], result["rows"]) == (120, 32)
    assert result["screen_revision"] == session.renderer.revision
    assert operator_result["screen_revision"] == session.renderer.revision
    assert session.attention is None
    assert session.state != "working"


async def _settle(clocked: Clocked, session: Any, output: str) -> None:
    """Emit output and let the fake clock reach its quiet boundary."""
    _manager, factory, _trigger, clock = clocked
    generation = session.activity_generation
    factory.adapters[0].emit(output)
    await settle_next_activity(
        clock, session, after_generation=generation, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
    )


async def _baseline(clocked: Clocked, tmp_path: Path) -> Any:
    manager, factory, trigger, clock = clocked
    return await establish_delivered_baseline(
        manager, factory, trigger, clock, tmp_path, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
    )


@pytest.mark.asyncio
async def test_operator_resize_burst_without_output_does_not_wake_agent(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    manager, _factory, trigger, clock = clocked_manager
    session = await _baseline(clocked_manager, tmp_path)
    revision = session.attention_revision

    for columns, rows in [(70, 20), (160, 48), (100, 30)]:
        await manager.resize_for_operator(session.terminal_id, columns=columns, rows=rows)
    await clock.advance(terminal_state.TERMINAL_RESIZE_GRACE_MAX_SECONDS + 1)

    assert len(trigger.submissions) == 1
    assert session.attention_revision == revision
    assert session.state == "ready"


@pytest.mark.asyncio
async def test_agent_input_clears_resize_grace_and_wakes_on_later_output(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """Input after a resize is work, so it ends the grace: a settle following
    it must deliver immediately instead of being treated as repaint noise."""
    manager, _factory, trigger, _clock = clocked_manager
    session = await _baseline(clocked_manager, tmp_path)
    await manager.resize(session.terminal_id, owner(), columns=100, rows=24)
    await _agent_input(manager, session, data="answer\r", origin_run_id="run-c")
    assert session.resize_grace_deadline == 0.0

    await _settle(clocked_manager, session, "output after agent input")

    await eventually(lambda: len(trigger.submissions) == 2)
    assert session.attention is not None
    assert session.attention.kind == "output_settled"


@pytest.mark.asyncio
async def test_unchanged_screen_stays_silent_until_content_changes(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """Suppression only covers an unchanged screen: once the screen actually
    changes, the next quiet boundary wakes the agent again."""
    _manager, _factory, trigger, _clock = clocked_manager
    session = await _baseline(clocked_manager, tmp_path)

    await _settle(clocked_manager, session, "\rMENU> ")
    assert len(trigger.submissions) == 1

    await _settle(clocked_manager, session, "\rMENU> \nsecond option")
    await eventually(lambda: len(trigger.submissions) == 2)
    assert session.attention is not None
    assert session.attention.kind == "output_settled"


@pytest.mark.asyncio
async def test_textless_agent_start_suppresses_the_startup_settle(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """A text-less Agent start stays silent until the first explicit input:
    the startup screen (banner, prompt, TUI boot) is observed by the starting
    Agent and must not wake the session."""
    manager, _factory, trigger, _clock = clocked_manager
    session = await spawn(manager, tmp_path)
    session.state = "working"

    await _settle(clocked_manager, session, "TUI banner")
    assert trigger.submissions == []
    assert session.attention is None or not session.attention.delivered
    await _settle(clocked_manager, session, "\rTUI banner")
    assert trigger.submissions == []

    await _agent_input(manager, session, data="go\r", origin_run_id="run-0")
    await _settle(clocked_manager, session, "output after input")
    await eventually(lambda: len(trigger.submissions) == 1)
    assert session.attention is not None
    assert session.attention.kind == "output_settled"


@pytest.mark.asyncio
async def test_resize_repaints_are_deferred_until_the_grace_cap_without_losing_final_content(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """Repaints after a resize wake nobody, yet the final screen is delivered at the
    grace cap without another PTY event, and later work wakes the Agent again."""
    manager, _factory, trigger, clock = clocked_manager
    session = await _baseline(clocked_manager, tmp_path)
    await manager.resize(session.terminal_id, owner(), columns=90, rows=24)

    for output in ("\rMENU> ", "\rMENU> status", "\rMENU> status", "\rMENU> status"):
        await _settle(clocked_manager, session, output)
        assert len(trigger.submissions) == 1

    await clock.advance(terminal_state.TERMINAL_RESIZE_GRACE_MAX_SECONDS)
    await eventually(lambda: len(trigger.submissions) == 2)
    await _settle(clocked_manager, session, "\rMENU> status\nnew work output line")
    await eventually(lambda: len(trigger.submissions) == 3)
    assert session.attention is not None
    assert session.attention.kind == "output_settled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "acknowledge"),
    [("\r\x1b[7mMENU> \x1b[0m", False), ("\r\x1b[2KTask completed. Please review result.", True)],
    ids=["styled-change-delivered", "acknowledged-screen-stays-silent"],
)
async def test_resize_final_output_is_delivered_once_unless_acknowledged(
    clocked_manager: Clocked, tmp_path: Path, output: str, acknowledge: bool
) -> None:
    manager, _factory, trigger, clock = clocked_manager
    session = await _baseline(clocked_manager, tmp_path)
    await manager.resize(session.terminal_id, owner(), columns=90, rows=24)
    await _settle(clocked_manager, session, output)
    assert len(trigger.submissions) == 1
    if acknowledge:
        manager.acknowledge_attention(session.terminal_id, owner(), session.attention_revision)

    await clock.advance(terminal_state.TERMINAL_RESIZE_GRACE_MAX_SECONDS)
    await eventually(lambda: session.settle_task is not None and session.settle_task.done())
    expected = 1 if acknowledge else 2
    await eventually(lambda: len(trigger.submissions) == expected)
    # The same screen again is not new work.
    await _settle(clocked_manager, session, output)
    assert len(trigger.submissions) == expected
