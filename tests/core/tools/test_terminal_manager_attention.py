"""Terminal manager: attention delivery to the attached Agent Session, and resize repaints."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

import core.tools._terminal_state as terminal_state
import core.tools.process_manager as process_manager
from core.runs import RunAdmissionBlockedError
from core.tools.terminal_manager import (
    TerminalClosedError,
    TerminalInfo,
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
    settle_next_activity,
    spawn,
    terminal_info,
)
from tests.core.tools.terminal_manager_helpers import clocked_manager as clocked_manager
from tests.core.tools.terminal_manager_helpers import default_shell as default_shell
from tests.core.tools.terminal_manager_helpers import delivering_manager as delivering_manager
from tests.core.tools.terminal_manager_helpers import shell_environment as shell_environment
from tests.core.tools.terminal_manager_helpers import terminal_manager as terminal_manager

Delivering = tuple[TerminalManager, AdapterFactory, PendingTriggerService]
Clocked = tuple[TerminalManager, AdapterFactory, PendingTriggerService, FakeClock]


async def _agent_input(
    manager: TerminalManager, terminal: TerminalInfo, **fields: Any
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "data": None,
        "text": None,
        "key": None,
        "expected_screen_revision": None,
        "origin_run_id": "run-b",
    }
    return await manager.send_input(terminal.terminal_id, owner(), **{**arguments, **fields})


@pytest.mark.asyncio
async def test_operator_activity_wakes_the_attached_agent_session(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    started = await spawn(manager, tmp_path)
    # A text-less Agent start suppresses the first settle (the startup screen
    # is not work); operator input re-arms delivery.
    await manager.send_operator_input(started.terminal_id, "look\r")
    factory.adapters[0].emit("screen changed")
    await eventually(lambda: terminal_info(manager, started.terminal_id).attention_revision == 1)

    settled = terminal_info(manager, started.terminal_id)
    assert settled.state == "ready"
    assert settled.attention is not None
    assert settled.attention.kind == "output_settled"
    await eventually(lambda: len(trigger.submissions) == 1)
    assert trigger.submissions[0][0] == ("agent-a", "session-a")


@pytest.mark.asyncio
async def test_wait_exit_and_explicit_kill_have_distinct_attention(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    natural = await spawn(manager, tmp_path)
    factory.adapters[0].finish(7)
    await eventually(lambda: terminal_info(manager, natural.terminal_id).state == "exited")
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
    started = await spawn(manager, tmp_path)
    await _agent_input(manager, started, text="do work", key="enter")
    factory.adapters[0].emit("working...\r\nREADY> ")
    await eventually(lambda: len(trigger.submissions) == 1)

    args, kwargs = trigger.submissions[0]
    assert args == ("agent-a", "session-a")
    assert kwargs["origin_run_id"] == "run-b"
    assert kwargs["project_id"] == "project-a"
    assert isinstance(kwargs["body"], str)
    assert started.terminal_id in kwargs["body"]
    assert "```" in kwargs["body"]
    assert "working..." in kwargs["body"]
    attention = terminal_info(manager, started.terminal_id).attention
    assert attention is not None
    assert attention.kind == "output_settled"

    manager.acknowledge_attention(started.terminal_id, owner(), 1)
    assert len(trigger.cancellations) == 1
    assert terminal_info(manager, started.terminal_id).acknowledged_attention_revision == 1
    # The automatic delivery stopped: releasing the trigger delivers nothing.
    trigger.release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    delivered = terminal_info(manager, started.terminal_id).attention
    assert delivered is not None
    assert not delivered.delivered
    assert len(trigger.cancellations) == 1


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
    started = await spawn(manager, tmp_path)
    await _agent_input(manager, started, text="do work", key="enter")
    factory.adapters[0].emit("working...\r\nREADY> ")
    await eventually(lambda: len(trigger.submissions) == 1)
    # The fake fails while submitting, so the delivery ends in the same step;
    # one more loop turn runs its completion logging.
    await asyncio.sleep(0)

    attention = terminal_info(manager, started.terminal_id).attention
    assert attention is not None
    assert attention.delivered is False
    # A closed execution owner is an expected lifecycle end, not an error.
    assert len(errors) == (0 if owner_closed else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["late output", "\x1b[1;1H"])
async def test_new_output_postpones_a_pending_agent_wakeup_to_the_next_quiet_boundary(
    delivering_manager: Delivering, tmp_path: Path, output: str
) -> None:
    manager, factory, trigger = delivering_manager
    started = await spawn(manager, tmp_path)
    await _agent_input(manager, started, data="begin")
    await eventually(lambda: len(trigger.submissions) == 1)

    factory.adapters[0].emit(output)
    await eventually(lambda: len(trigger.cancellations) == 1)
    await eventually(lambda: len(trigger.submissions) == 2)

    postponed = terminal_info(manager, started.terminal_id)
    assert postponed.attention_revision == 2
    assert postponed.attention is not None
    assert postponed.attention.kind == "output_settled"
    assert trigger.submissions[0][1]["notice_id"] != trigger.submissions[1][1]["notice_id"]
    assert trigger.submissions[1][1]["origin_run_id"] == "run-b"


@pytest.mark.asyncio
async def test_session_move_reroutes_pending_attention_to_new_owner(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, _factory, trigger = delivering_manager
    started = await spawn(manager, tmp_path)
    await _agent_input(manager, started, text="do work", key="enter")
    await eventually(lambda: len(trigger.submissions) == 1)
    target = TerminalOwner("project-b", "agent-b", "session-a")

    assert manager.transfer_scope(owner(), target) == 1
    await eventually(lambda: len(trigger.submissions) == 2)

    assert trigger.submissions[0][0] == ("agent-a", "session-a")
    assert trigger.submissions[1][0] == ("agent-b", "session-a")
    assert trigger.submissions[1][1]["project_id"] == "project-b"
    assert len(trigger.cancellations) == 1
    assert manager.terminal(started.terminal_id, target).attachment == target


@pytest.mark.asyncio
async def test_notification_revision_authorizes_only_the_delivered_screen(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """The screen revision a notification names guards input against a changed
    screen. An operator resize and the program's redraw for the new size keep
    it valid; input and new program output invalidate it."""
    manager, factory, trigger, clock = clocked_manager
    delivered = await establish_delivered_baseline(
        manager, factory, trigger, clock, tmp_path, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
    )
    terminal_id = delivered.terminal_id
    adapter = factory.adapters[0]
    body = trigger.submissions[0][1]["body"]
    match = re.search(r"^screen_revision: (\d+)$", body, re.MULTILINE)
    assert match is not None
    revision = int(match[1])
    assert revision == delivered.screen_revision
    assert "MENU>" in body

    await manager.resize_for_operator(terminal_id, columns=100, rows=30)
    adapter.emit("\x1b[2J\x1b[HMENU> ")
    # The redraw inside the repaint window restarted the quiet timer.
    await eventually(lambda: clock.sleeping)
    assert terminal_info(manager, terminal_id).screen_revision == revision

    sent = await _agent_input(
        manager, delivered, text="y", key="enter", expected_screen_revision=revision
    )
    assert sent["characters_sent"] == 2
    assert adapter.writes[-2:] == ["y", "\r"]
    writes = list(adapter.writes)
    # The answer changed the screen revision before any echo arrived, so a replay is refused.
    with pytest.raises(TerminalStaleScreenError):
        await _agent_input(
            manager, delivered, text="y", key="enter", expected_screen_revision=revision
        )
    # New program output, unlike a redraw, invalidates the revision the answer returned.
    adapter.emit("\r\nApplied.")
    await eventually(
        lambda: terminal_info(manager, terminal_id).screen_revision > sent["screen_revision"]
    )
    with pytest.raises(TerminalStaleScreenError):
        await _agent_input(
            manager, delivered, data="n\r", expected_screen_revision=sent["screen_revision"]
        )
    assert adapter.writes == writes


@pytest.mark.asyncio
async def test_closed_pty_write_marks_terminal_exited_and_delivers_attention(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    started = await spawn(manager, tmp_path)
    manager.attach(started.terminal_id, owner(), origin_run_id="attach-run")
    factory.adapters[0].write_error = EOFError("Pty is closed")

    with pytest.raises(TerminalClosedError):
        await manager.send_operator_input(started.terminal_id, "late input")

    await eventually(lambda: len(trigger.submissions) == 1)
    closed = terminal_info(manager, started.terminal_id)
    assert closed.state == "exited"
    assert closed.attention is not None
    assert closed.attention.kind == "exited"
    assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"


@pytest.mark.asyncio
async def test_unattached_operator_terminal_has_no_agent_scope_or_attention_delivery(
    delivering_manager: Delivering, tmp_path: Path, default_shell: str
) -> None:
    manager, factory, trigger = delivering_manager
    result = await manager.spawn_for_operator(command=None, arguments=["--login"], cwd=tmp_path)
    terminal_id = result["terminal_id"]

    assert factory.calls[0][0] == [default_shell, "--login"]
    assert (result["owner"], result["attachment"]) == (None, None)
    started = terminal_info(manager, terminal_id)
    assert (started.owner, started.lifecycle_owner, started.attachment) == (None, None, None)

    await manager.close_project_scope("project-a")
    assert factory.adapters[0].alive is True

    await manager.send_operator_input(terminal_id, "echo ready\r")
    factory.adapters[0].emit("ready\r\n")
    await eventually(lambda: terminal_info(manager, terminal_id).state == "ready")
    factory.adapters[0].finish(0)
    await eventually(lambda: terminal_info(manager, terminal_id).state == "exited")
    assert trigger.submissions == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("default_shell")
async def test_operator_terminal_attach_delivers_activity_and_detach_preserves_lifetime(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, factory, trigger = delivering_manager
    result = await manager.spawn_for_operator(
        command=None, arguments=[], cwd=tmp_path, columns=137, rows=41
    )
    terminal_id = result["terminal_id"]

    attached, changed = manager.attach(terminal_id, owner(), origin_run_id="attach-run")
    assert (attached.terminal_id, changed) == (terminal_id, True)
    assert (attached.owner, attached.lifecycle_owner, attached.attachment) == (None, None, owner())
    assert manager.terminal(terminal_id, owner()).attachment == owner()
    same, changed = manager.attach(terminal_id, owner(), origin_run_id="attach-run-2")
    assert (same.attachment, changed) == (owner(), False)
    # Attaching adopts the operator's size instead of resizing the program.
    assert (same.columns, same.rows) == (137, 41)
    assert factory.adapters[0].resizes == []

    await manager.send_operator_input(terminal_id, "echo ready\r")
    factory.adapters[0].emit("ready\r\n")
    await eventually(lambda: len(trigger.submissions) == 1)
    assert trigger.submissions[0][0] == ("agent-a", "session-a")
    assert trigger.submissions[0][1]["origin_run_id"] == "attach-run-2"

    detached = manager.detach(terminal_id, owner())
    assert (detached.terminal_id, detached.attachment) == (terminal_id, None)
    with pytest.raises(TerminalNotOwnedError):
        manager.terminal(terminal_id, owner())
    await manager.close_scope(owner())
    assert factory.adapters[0].alive is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("default_shell")
async def test_attach_arms_an_already_working_terminal_for_its_next_quiet_boundary(
    delivering_manager: Delivering, tmp_path: Path
) -> None:
    manager, _factory, trigger = delivering_manager
    result = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
    terminal_id = result["terminal_id"]
    # The operator starts work that no Agent is attached to yet.
    await manager.send_operator_input(terminal_id, "make\r")
    assert terminal_info(manager, terminal_id).state == "working"

    manager.attach(terminal_id, owner(), origin_run_id="attach-run")

    await eventually(lambda: len(trigger.submissions) == 1)
    attention = terminal_info(manager, terminal_id).attention
    assert attention is not None
    assert attention.kind == "output_settled"
    assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"


@pytest.mark.asyncio
async def test_resize_to_current_dimensions_is_a_no_op(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)

    result = await manager.resize(started.terminal_id, owner(), columns=120, rows=32)
    operator_result = await manager.resize_for_operator(started.terminal_id, columns=120, rows=32)

    assert factory.adapters[0].resizes == []
    assert (result["columns"], result["rows"]) == (120, 32)
    assert result["screen_revision"] == started.screen_revision
    assert operator_result["screen_revision"] == started.screen_revision
    unchanged = terminal_info(manager, started.terminal_id)
    assert unchanged.attention is None
    assert unchanged.state != "working"


async def _settle(clocked: Clocked, terminal_id: str, output: str) -> None:
    """Emit output that changes the screen and let it reach its quiet boundary."""
    manager, factory, _trigger, clock = clocked
    revision = terminal_info(manager, terminal_id).screen_revision
    factory.adapters[0].emit(output)
    await settle_next_activity(
        clock,
        manager,
        terminal_id,
        after_revision=revision,
        quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
    )


async def _repaint(clocked: Clocked, terminal_id: str, columns: int, rows: int) -> None:
    """Resize a quiet terminal and let the program's redraw reach its quiet boundary."""
    manager, factory, _trigger, clock = clocked
    await manager.resize_for_operator(terminal_id, columns=columns, rows=rows)
    factory.adapters[0].emit("\x1b[2J\x1b[HMENU> ")
    # A redraw keeps the screen revision; it only restarts the quiet timer.
    await eventually(lambda: clock.sleeping)
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)


async def _baseline(clocked: Clocked, tmp_path: Path) -> TerminalInfo:
    manager, factory, trigger, clock = clocked
    return await establish_delivered_baseline(
        manager, factory, trigger, clock, tmp_path, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
    )


@pytest.mark.asyncio
async def test_repaint_after_resizing_a_quiet_terminal_does_not_wake_agent(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """A quiet program redraws its known content for each new size. The redraw
    wakes nobody and becomes the baseline; only new content wakes the Agent."""
    manager, _factory, trigger, _clock = clocked_manager
    delivered = await _baseline(clocked_manager, tmp_path)
    terminal_id = delivered.terminal_id

    for columns, rows in [(70, 20), (160, 48), (100, 30)]:
        await _repaint(clocked_manager, terminal_id, columns, rows)
    repainted = terminal_info(manager, terminal_id)
    assert repainted.attention_revision == delivered.attention_revision
    assert repainted.screen_revision == delivered.screen_revision
    assert repainted.state == "ready"
    # The same screen again, long after the resize, is still known content.
    await _settle(clocked_manager, terminal_id, "\x1b[2J\x1b[HMENU> ")
    assert len(trigger.submissions) == 1

    await _settle(clocked_manager, terminal_id, "\r\nTask completed.")
    await eventually(lambda: len(trigger.submissions) == 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    ["output-after-window", "input-then-resize", "resize-then-input"],
)
async def test_activity_around_a_resize_wakes_agent(
    clocked_manager: Clocked, tmp_path: Path, order: str
) -> None:
    """Output after the repaint window, and anything following input, is
    activity even when it arrives right after a resize."""
    manager, factory, trigger, clock = clocked_manager
    delivered = await _baseline(clocked_manager, tmp_path)
    terminal_id = delivered.terminal_id
    if order == "output-after-window":
        await manager.resize(terminal_id, owner(), columns=100, rows=24)
        await clock.advance(terminal_state.TERMINAL_REPAINT_WINDOW_SECONDS + 0.1)
    elif order == "input-then-resize":
        await _agent_input(manager, delivered, data="answer\r", origin_run_id="run-c")
        await manager.resize(terminal_id, owner(), columns=100, rows=24)
    else:
        await manager.resize(terminal_id, owner(), columns=100, rows=24)
        await _agent_input(manager, delivered, data="answer\r", origin_run_id="run-c")

    await _settle(clocked_manager, terminal_id, "\r\nTask completed.")

    await eventually(lambda: len(trigger.submissions) == 2)
    attention = terminal_info(manager, terminal_id).attention
    assert attention is not None
    assert attention.kind == "output_settled"
    assert factory.adapters[0].resizes == [(24, 100)]


@pytest.mark.asyncio
async def test_unchanged_screen_stays_silent_until_content_changes(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """Suppression only covers an unchanged screen: once the screen actually
    changes, the next quiet boundary wakes the agent again."""
    manager, _factory, trigger, _clock = clocked_manager
    delivered = await _baseline(clocked_manager, tmp_path)

    await _settle(clocked_manager, delivered.terminal_id, "\rMENU> ")
    assert len(trigger.submissions) == 1

    await _settle(clocked_manager, delivered.terminal_id, "\rMENU> \nsecond option")
    await eventually(lambda: len(trigger.submissions) == 2)
    attention = terminal_info(manager, delivered.terminal_id).attention
    assert attention is not None
    assert attention.kind == "output_settled"


@pytest.mark.asyncio
async def test_textless_agent_start_suppresses_the_startup_settle(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """A text-less Agent start stays silent until the first explicit input:
    the startup screen (banner, prompt, TUI boot) is observed by the starting
    Agent and must not wake the session."""
    manager, _factory, trigger, _clock = clocked_manager
    started = await spawn(manager, tmp_path)
    terminal_id = started.terminal_id

    await _settle(clocked_manager, terminal_id, "TUI banner")
    assert trigger.submissions == []
    startup = terminal_info(manager, terminal_id).attention
    assert startup is None or not startup.delivered
    await _settle(clocked_manager, terminal_id, "\rTUI banner")
    assert trigger.submissions == []

    await _agent_input(manager, started, data="go\r", origin_run_id="run-0")
    await _settle(clocked_manager, terminal_id, "output after input")
    await eventually(lambda: len(trigger.submissions) == 1)
    attention = terminal_info(manager, terminal_id).attention
    assert attention is not None
    assert attention.kind == "output_settled"
