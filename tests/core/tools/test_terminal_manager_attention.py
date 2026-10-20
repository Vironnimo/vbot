"""Terminal manager: attention delivery to the attached Agent Session, and resize repaints."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

import core.tools._terminal_output as terminal_output
import core.tools._terminal_session as terminal_session
import core.tools._terminal_state as terminal_state
from core.runs import RunAdmissionBlockedError
from core.sessions import SessionAddress
from core.storage.temp_files import TemporaryFileManager
from core.tools._terminal_activity import PROCESS_SAMPLE_SECONDS
from core.tools._terminal_render_host import TerminalScreen
from core.tools.process_tree import RunningProcess
from core.tools.terminal_manager import (
    TerminalClosedError,
    TerminalInfo,
    TerminalManager,
    TerminalManagerError,
    TerminalNotOwnedError,
    TerminalOwner,
    TerminalRenderHost,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_helpers import call, make_context
from tests.core.tools.terminal_manager_helpers import (
    TEST_ACTIVITY_QUIET_SECONDS,
    TEST_IDLE_SECONDS,
    TEST_START_WAIT_SECONDS,
    AdapterFactory,
    FakeClock,
    FakeTree,
    PendingTriggerService,
    TrackedExecutor,
    establish_delivered_baseline,
    eventually,
    owner,
    settle,
    settle_next_activity,
    spawn,
    terminal_info,
)
from tests.core.tools.terminal_manager_helpers import clocked_manager as clocked_manager
from tests.core.tools.terminal_manager_helpers import default_shell as default_shell
from tests.core.tools.terminal_manager_helpers import delivering_manager as delivering_manager
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
    await manager.send_operator_input(started.terminal_id, "look\r")
    factory.adapters[0].emit("screen changed")
    await eventually(lambda: terminal_info(manager, started.terminal_id).attention_revision == 1)

    settled = terminal_info(manager, started.terminal_id)
    assert settled.state == "ready"
    assert settled.attention is not None
    assert settled.attention.kind == "idle"
    await eventually(lambda: len(trigger.submissions) == 1)
    assert trigger.submissions[0][0] == (owner().address,)


@pytest.mark.asyncio
async def test_wait_exit_and_explicit_kill_have_distinct_attention(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    natural = await spawn(manager, tmp_path)
    factory.adapters[0].emit("goodbye\r\n")
    factory.adapters[0].finish(7)
    assert await manager.wait(natural.terminal_id, owner(), seconds=10) == "exited"
    snapshot = await manager.snapshot(natural.terminal_id, owner())
    assert snapshot["attention"]["kind"] == "exited"
    assert (snapshot["exit_code"], snapshot["stopped"]) == (7, False)
    assert snapshot["screen"] == "goodbye"

    killed = await spawn(manager, tmp_path)
    result = await manager.kill(killed.terminal_id, owner())
    assert (result.state, result.stopped, result.attention) == ("exited", True, None)


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
    assert args == (owner().address,)
    assert kwargs["origin_run_id"] == "run-b"
    terminal_id = started.terminal_id
    body = kwargs["body"]
    assert body.startswith(f"Terminal {terminal_id} (Anna, fake-tui) went idle")
    assert "\n```text\nworking...\nREADY>\n```\n" in body
    assert f'terminal_id "{terminal_id}"' in body
    attention = terminal_info(manager, started.terminal_id).attention
    assert attention is not None
    assert (attention.kind, attention.details["reason"]) == ("idle", "quiet")

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
        terminal_session._LOGGER, "error", lambda *args, **_kwargs: errors.append(args)
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
async def test_new_output_postpones_a_pending_agent_wakeup_to_the_next_idle_boundary(
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
    assert postponed.attention.kind == "idle"
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

    assert await manager.transfer_scope(owner(), target) == 1
    await eventually(lambda: len(trigger.submissions) == 2)

    assert trigger.submissions[0][0] == (owner().address,)
    assert trigger.submissions[1][0] == (SessionAddress("project-b", "agent-b", "session-a"),)
    assert len(trigger.cancellations) == 1
    assert manager.terminal(started.terminal_id, target).attachment == target


@pytest.mark.asyncio
async def test_notification_revision_authorizes_only_the_delivered_screen(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """The screen revision of a delivered screen guards input against a changed
    screen. An operator resize and the program's redraw for the new size keep
    it valid; input and new program output invalidate it."""
    manager, factory, trigger, clock = clocked_manager
    delivered = await establish_delivered_baseline(
        manager, factory, trigger, clock, tmp_path, quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS
    )
    terminal_id = delivered.terminal_id
    adapter = factory.adapters[0]
    assert "MENU>" in trigger.submissions[0][1]["body"]
    revision = delivered.screen_revision

    await manager.resize_for_operator(terminal_id, columns=100, rows=30)
    adapter.emit("\x1b[2J\x1b[HMENU> ")
    # The redraw inside the repaint window restarted the quiet timer.
    await eventually(lambda: clock.sleeping)
    assert terminal_info(manager, terminal_id).screen_revision == revision

    answer = asyncio.ensure_future(
        _agent_input(manager, delivered, text="y", key="enter", expected_screen_revision=revision)
    )
    # Enter follows once the program took the text.
    await eventually(lambda: adapter.writes[-1:] == ["y"])
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS / 8)
    sent = await answer
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


@pytest.mark.parametrize("writer", ["operator", "agent", "cancelled-agent", "stopped"])
@pytest.mark.asyncio
async def test_refused_write_ends_the_terminal_only_once_its_program_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        temporary_files=TemporaryFileManager(tmp_path / "temp"),
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
        idle_seconds=0.06,
        start_wait_seconds=TEST_START_WAIT_SECONDS,
    )
    manager.start()
    try:
        started = await spawn(manager, tmp_path)
        manager.attach(started.terminal_id, owner(), origin_run_id="attach-run")
        adapter = factory.adapters[0]
        reading = threading.Event()
        release = threading.Event()
        failed = threading.Event()
        read = adapter.read

        def delayed_read(size: int) -> str:
            output = read(size)
            if output == "final words\r\n":
                reading.set()
                assert release.wait(5), "test did not release the last output"
            return output

        monkeypatch.setattr(adapter, "read", delayed_read)
        adapter.emit("last words\r\n")
        await eventually(lambda: terminal_info(manager, started.terminal_id).screen_revision > 0)

        def unwritable(text: str) -> None:
            raise OSError(5, f"Input/output error writing {text!r}")

        # A write the PTY refuses while the program still runs ends nothing.
        adapter.write = unwritable  # type: ignore[method-assign]
        with pytest.raises(TerminalManagerError, match="could not be written") as refused:
            await manager.send_operator_input(started.terminal_id, "early input")
        assert not isinstance(refused.value, TerminalClosedError)
        assert terminal_info(manager, started.terminal_id).finished_at is None
        del adapter.write

        def closed_write(text: str) -> None:
            failed.set()
            raise EOFError("Pty is closed")

        monkeypatch.setattr(adapter, "write", closed_write)
        adapter.emit("final words\r\n")
        try:
            await eventually(reading.is_set)
            adapter.finish(7)
            draining = asyncio.Event()
            if writer == "stopped":
                wait = asyncio.wait

                async def observe_drain(futures: Any, **kwargs: Any) -> Any:
                    if len(futures) == 1:
                        draining.set()
                    return await wait(futures, **kwargs)

                monkeypatch.setattr(terminal_session.asyncio, "wait", observe_drain)
            sending = asyncio.create_task(
                manager.send_operator_input(started.terminal_id, "late input")
                if writer == "operator"
                else _agent_input(manager, started, data="late input")
            )
            await eventually(failed.is_set)
            assert not sending.done()
            assert terminal_info(manager, started.terminal_id).finished_at is None
            assert not adapter.closed
            if writer == "cancelled-agent":
                sending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await sending
            elif writer == "stopped":
                await draining.wait()
                wait_for = asyncio.wait_for

                async def stop_drain_timeout(awaitable: Any, timeout: float | None) -> Any:
                    if timeout == 5:
                        # Stop exhausted its drain allowance. Cancelling the
                        # shield leaves terminate responsible for its reader.
                        awaitable.cancel()
                        raise TimeoutError
                    return await wait_for(awaitable, timeout)

                with monkeypatch.context() as stopping:
                    stopping.setattr(terminal_session.asyncio, "wait_for", stop_drain_timeout)
                    stopped = await manager.kill(started.terminal_id, owner())
                assert stopped.stopped
                assert sending.cancelling() == 0
        finally:
            release.set()
        if writer != "cancelled-agent":
            with pytest.raises(TerminalClosedError):
                await sending
        await eventually(lambda: terminal_info(manager, started.terminal_id).state == "exited")

        closed = terminal_info(manager, started.terminal_id)
        assert closed.state == "exited"
        snapshot = await manager.snapshot(started.terminal_id, owner())
        assert snapshot["exit_code"] == 7
        assert snapshot["log_complete"] is True
        if writer == "stopped":
            # An explicit stop cancels a stuck read; the earlier logged output
            # stays available, and its waiting input is closed, not cancelled.
            assert snapshot["stopped"] is True
            assert Path(snapshot["log_file"]).read_bytes() == b"last words\r\n"
            return
        assert closed.attention is not None
        assert closed.attention.kind == "exited"
        assert Path(snapshot["log_file"]).read_bytes() == b"last words\r\nfinal words\r\n"
        notice_id = closed.attention.notice_id
        await eventually(
            lambda: any(kw["notice_id"] == notice_id for _a, kw in trigger.submissions)
        )
        exit_message = next(kw for _a, kw in trigger.submissions if kw["notice_id"] == notice_id)
        assert exit_message["origin_run_id"] == ("attach-run" if writer == "operator" else "run-b")
        # The exit message carries the last screen.
        assert exit_message["body"].endswith("\n```text\nlast words\nfinal words\n```")

    finally:
        await manager.aclose()


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
    assert trigger.submissions[0][0] == (owner().address,)
    assert trigger.submissions[0][1]["origin_run_id"] == "attach-run-2"

    detached = manager.detach(terminal_id, owner())
    assert (detached.terminal_id, detached.attachment) == (terminal_id, None)
    with pytest.raises(TerminalNotOwnedError):
        manager.terminal(terminal_id, owner())
    await manager.close_scope(owner())
    assert factory.adapters[0].alive is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("default_shell")
async def test_attach_arms_an_already_working_terminal_for_its_next_idle_boundary(
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
    assert attention.kind == "idle"
    assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"


@pytest.mark.asyncio
async def test_resize_to_current_dimensions_is_a_no_op(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)

    result = await manager.resize_for_operator(started.terminal_id, columns=120, rows=32)

    assert factory.adapters[0].resizes == []
    assert (result["columns"], result["rows"]) == (120, 32)
    assert result["screen_revision"] == started.screen_revision
    unchanged = terminal_info(manager, started.terminal_id)
    assert unchanged.attention is None
    assert unchanged.state != "working"


async def _idle(clocked: Clocked, terminal_id: str, output: str) -> None:
    """Emit output that changes the screen and let it reach its idle boundary."""
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


async def _unchanged(clocked: Clocked, terminal_id: str, output: str) -> None:
    """Emit output that draws the screen as it was: it is no work and wakes nobody."""
    manager, factory, _trigger, _clock = clocked
    revision = terminal_info(manager, terminal_id).screen_revision
    factory.adapters[0].emit(output)
    await eventually(lambda: terminal_info(manager, terminal_id).screen_revision > revision)
    await _turns()
    assert terminal_info(manager, terminal_id).state == "ready"


async def _turns() -> None:
    """Let a boundary that is due run; the in-process screen needs no thread."""
    for _turn in range(10):
        await asyncio.sleep(0)


async def _repaint(clocked: Clocked, terminal_id: str, columns: int, rows: int) -> None:
    """Resize a quiet terminal and let the program's redraw reach its quiet boundary."""
    manager, factory, _trigger, clock = clocked
    await manager.resize_for_operator(terminal_id, columns=columns, rows=rows)
    factory.adapters[0].emit("\x1b[2J\x1b[HMENU> ")
    # A redraw keeps the screen revision; it only restarts the activity timer.
    await eventually(lambda: clock.sleeping)
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
    await _turns()


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
    await _unchanged(clocked_manager, terminal_id, "\x1b[2J\x1b[HMENU> ")
    assert len(trigger.submissions) == 1

    await _idle(clocked_manager, terminal_id, "\r\nTask completed.")
    await eventually(lambda: len(trigger.submissions) == 2)
    assert "Task completed." in trigger.submissions[1][1]["body"]


@pytest.mark.asyncio
async def test_a_pointer_passing_over_the_terminal_is_no_work(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """A program tracking every mouse event receives the motion of a pointer
    that merely passes over its tile. That keeps the terminal ready and the
    screen revision, and the program's hover redraw wakes nobody; a click is
    input."""
    manager, factory, trigger, clock = clocked_manager
    delivered = await _baseline(clocked_manager, tmp_path)
    terminal_id = delivered.terminal_id
    motion = "\x1b[<35;4;1M\x1b[<35;5;1M"

    await manager.send_operator_input(terminal_id, motion)
    await eventually(lambda: motion in factory.adapters[0].writes)
    assert terminal_info(manager, terminal_id).screen_revision == delivered.screen_revision
    factory.adapters[0].emit("\x1b[2J\x1b[H\x1b[7mMENU>\x1b[0m ")
    await eventually(lambda: clock.sleeping)
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
    await _turns()
    hovered = terminal_info(manager, terminal_id)
    assert (hovered.state, hovered.attention_revision) == ("ready", delivered.attention_revision)
    assert len(trigger.submissions) == 1

    await manager.send_operator_input(terminal_id, "\x1b[<0;4;1M")
    assert terminal_info(manager, terminal_id).state == "working"


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["declared", "fallback"])
async def test_work_the_program_resumed_on_its_own_wakes_at_a_declared_end_or_the_fallback(
    clocked_manager: Clocked, tmp_path: Path, end: str
) -> None:
    """Repeated bursts after a quick idle can resume the same work, each
    settling promptly. Work that resumes later on its own can pause:
    its pauses wake nobody. A declared end (here a bell) wakes the Agent at
    the next quiet boundary; without one, the fallback wakes it once the
    program printed nothing for the idle period."""
    manager, factory, trigger, clock = clocked_manager
    delivered = await _baseline(clocked_manager, tmp_path)
    terminal_id = delivered.terminal_id

    async def emit(output: str) -> None:
        revision = terminal_info(manager, terminal_id).screen_revision
        factory.adapters[0].emit(output)
        await eventually(lambda: terminal_info(manager, terminal_id).screen_revision > revision)
        await eventually(lambda: clock.sleeping)

    for burst in range(3):
        await emit(f"\r\nresumed {burst}")
        for _chunk in range(2):
            await clock.advance(TEST_ACTIVITY_QUIET_SECONDS / 4)
            await emit(" more")
        await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
        await eventually(lambda burst=burst: len(trigger.submissions) == burst + 2)
        resumed = terminal_info(manager, terminal_id).attention
        assert resumed is not None
        assert resumed.details["reason"] == "quiet"

    await clock.advance(TEST_IDLE_SECONDS)
    for step in range(3):
        await emit(f"\r\nstep {step}")
        await clock.advance(TEST_IDLE_SECONDS - 0.5)
        await _turns()
        assert len(trigger.submissions) == 4
        assert terminal_info(manager, terminal_id).state == "working"

    await emit("\r\ndone\x07" if end == "declared" else "\r\ndone")
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
    await _turns()
    if end == "fallback":
        assert len(trigger.submissions) == 4
        await clock.advance(TEST_IDLE_SECONDS - TEST_ACTIVITY_QUIET_SECONDS)
    await eventually(lambda: len(trigger.submissions) == 5)
    attention = terminal_info(manager, terminal_id).attention
    assert attention is not None
    assert attention.details["reason"] == end
    body = trigger.submissions[4][1]["body"]
    assert "step 2\ndone" in body
    assert (f"printed nothing for {TEST_IDLE_SECONDS:g} seconds" in body) == (end == "fallback")


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [1, 3])
async def test_periodic_changed_redraws_do_not_wake_the_agent(
    clocked_manager: Clocked, tmp_path: Path, chunks: int
) -> None:
    """A changed clock frame is no fresh work, including the first refresh
    after startup and frames split across several transport reads."""
    manager, factory, trigger, clock = clocked_manager
    delivered = await _baseline(clocked_manager, tmp_path)
    terminal_id = delivered.terminal_id
    for frame in range(4):
        for chunk in range(chunks):
            if chunk:
                await clock.advance(TEST_ACTIVITY_QUIET_SECONDS / 10)
            revision = terminal_info(manager, terminal_id).screen_revision
            factory.adapters[0].emit(f"\rclock {frame}.{chunk}")
            await eventually(
                lambda revision=revision: (
                    terminal_info(manager, terminal_id).screen_revision > revision
                )
            )
            await eventually(lambda: clock.sleeping)
        await clock.advance(TEST_ACTIVITY_QUIET_SECONDS * 2)
        await _turns()
        assert len(trigger.submissions) == 1
        assert terminal_info(manager, terminal_id).state == "working"

    # Once even the periodic redraw ends, its final screen remains observable.
    await clock.advance(TEST_IDLE_SECONDS - TEST_ACTIVITY_QUIET_SECONDS * 2)
    await eventually(lambda: len(trigger.submissions) == 2)
    attention = terminal_info(manager, terminal_id).attention
    assert attention is not None and attention.details["reason"] == "fallback"


@pytest.mark.asyncio
async def test_a_blinking_view_is_no_work_while_it_toggles(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """A program that flips its view back within the toggle period, as a
    blinking cursor does, shows no work: the reply to input ends at the quiet
    boundary after its last real change. A view that comes back later is work."""
    manager, factory, _trigger, clock = clocked_manager
    delivered = await _baseline(clocked_manager, tmp_path)
    terminal_id = delivered.terminal_id
    adapter = factory.adapters[0]
    # A drawn cursor in the top left corner; the terminal's cursor stays put.
    cursor_on, cursor_off = "\x1b7\x1b[H\x1b[7mX\x1b[0m\x1b8", "\x1b7\x1b[HX\x1b8"

    async def show(output: str) -> None:
        revision = terminal_info(manager, terminal_id).screen_revision
        adapter.emit(output)
        await eventually(lambda: terminal_info(manager, terminal_id).screen_revision > revision)

    sent = await _agent_input(manager, delivered, data="x")
    # The reply draws both phases of the cursor once: real changes.
    await show(cursor_on)
    await show(cursor_off)
    await eventually(lambda: clock.sleeping)
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS / 2)
    await show(cursor_on)
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS / 2)
    await _turns()
    # One quiet period after the last real change, although the view kept toggling.
    assert terminal_info(manager, terminal_id).state == "ready"
    ended = await manager.wait_for_reply(
        terminal_id, owner(), seconds=0, after_quiet=int(sent["quiet_boundaries"])
    )
    assert ended == "quiet"

    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS * 2)
    await show(cursor_off)
    assert terminal_info(manager, terminal_id).state == "working"


@pytest.mark.asyncio
@pytest.mark.parametrize("work", ["child", "cpu"])
async def test_work_the_processes_show_keeps_a_quiet_program_working(
    tmp_path: Path, work: str
) -> None:
    """A program can work without printing: in a process its input started, or
    on the CPU. Its quiet output wakes nobody while that work runs; the first
    process sample after the work ended goes idle, long before the fallback."""
    clock = FakeClock()
    executor = TrackedExecutor()
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    tree = FakeTree()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        activity_quiet_seconds=TEST_ACTIVITY_QUIET_SECONDS,
        idle_seconds=TEST_IDLE_SECONDS,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        process_observer=lambda _pid: tree,
    )
    asyncio.get_running_loop().set_default_executor(executor)
    manager.start()
    try:
        started = await spawn(manager, tmp_path)
        await _agent_input(manager, started, data="make\r")
        for cycle in range(3):
            began = clock.now
            revision = terminal_info(manager, started.terminal_id).screen_revision
            factory.adapters[0].emit(f"\r\ncompiling {cycle}...")
            await eventually(
                lambda revision=revision: (
                    terminal_info(manager, started.terminal_id).screen_revision > revision
                )
            )
            if work == "child":
                tree.running = (*tree.running, RunningProcess(cycle + 2, "make.exe"))
                tree.started += 1

            async def run_until(seconds: float, cycle: int = cycle, began: float = began) -> None:
                while clock.now < seconds and len(trigger.submissions) == cycle:
                    await settle(clock, executor)
                    if work == "cpu" and clock.now < began + 3:
                        tree.cpu_seconds += PROCESS_SAMPLE_SECONDS
                    await clock.advance(PROCESS_SAMPLE_SECONDS)
                await settle(clock, executor)

            await run_until(began + 3)
            assert len(trigger.submissions) == cycle
            assert terminal_info(manager, started.terminal_id).state == "working"

            tree.running = tree.running[:1]
            await run_until(began + TEST_IDLE_SECONDS - 0.5)
            assert len(trigger.submissions) == cycle + 1
            attention = terminal_info(manager, started.terminal_id).attention
            assert attention is not None
            assert attention.details["reason"] == "quiet"
    finally:
        await manager.aclose()


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
        await manager.resize_for_operator(terminal_id, columns=100, rows=24)
        await clock.advance(terminal_state.TERMINAL_REPAINT_WINDOW_SECONDS + 0.1)
    elif order == "input-then-resize":
        await _agent_input(manager, delivered, data="answer\r", origin_run_id="run-c")
        await manager.resize_for_operator(terminal_id, columns=100, rows=24)
    else:
        await manager.resize_for_operator(terminal_id, columns=100, rows=24)
        await _agent_input(manager, delivered, data="answer\r", origin_run_id="run-c")

    await _idle(clocked_manager, terminal_id, "\r\nTask completed.")

    await eventually(lambda: len(trigger.submissions) == 2)
    attention = terminal_info(manager, terminal_id).attention
    assert attention is not None
    assert attention.kind == "idle"
    assert factory.adapters[0].resizes == [(24, 100)]


@pytest.mark.asyncio
async def test_unchanged_screen_stays_silent_until_content_changes(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """Suppression only covers an unchanged screen: once the screen actually
    changes, the next idle boundary wakes the agent again."""
    manager, _factory, trigger, _clock = clocked_manager
    delivered = await _baseline(clocked_manager, tmp_path)

    await _unchanged(clocked_manager, delivered.terminal_id, "\rMENU> ")
    assert len(trigger.submissions) == 1

    await _idle(clocked_manager, delivered.terminal_id, "\rMENU> \nsecond option")
    await eventually(lambda: len(trigger.submissions) == 2)
    assert "second option" in trigger.submissions[1][1]["body"]
    attention = terminal_info(manager, delivered.terminal_id).attention
    assert attention is not None
    assert attention.kind == "idle"


@pytest.mark.asyncio
async def test_a_start_result_keeps_only_the_screen_it_showed_from_being_delivered(
    clocked_manager: Clocked, tmp_path: Path
) -> None:
    """The first screen the start result showed is not delivered again, also when
    its idle boundary came before the result was durable; the program's later
    screens are, start-up screens included."""
    manager, factory, trigger, clock = clocked_manager
    factory.initial_output = "TUI banner"
    persisted: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=persisted.append)
    starting = asyncio.ensure_future(call(manager, context, {"action": "start", "command": "tui"}))
    await eventually(lambda: clock.sleeping)
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
    result = await starting
    terminal_id = result["data"]["terminal_id"]
    assert result["data"]["screen"] == "TUI banner"
    for acknowledge in persisted:
        acknowledge()
    # Its idle boundary had scheduled the screen; the durable result cancelled it.
    assert len(trigger.cancellations) == len(trigger.submissions)
    await _unchanged(clocked_manager, terminal_id, "\rTUI banner")
    assert len(trigger.cancellations) == len(trigger.submissions)

    shown = len(trigger.submissions)
    await _idle(clocked_manager, terminal_id, "\r\nloaded")
    await eventually(lambda: len(trigger.submissions) == shown + 1)
    assert "loaded" in trigger.submissions[-1][1]["body"]
    assert len(trigger.cancellations) == shown


@pytest.mark.parametrize("failing", ["read", "render"])
@pytest.mark.asyncio
async def test_terminal_failure_keeps_the_program_until_it_ends_then_delivers_its_end(
    clocked_manager: Clocked,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
) -> None:
    """A failure to read the output ends the terminal as failed; a screen that
    misses output (here without a log to draw it anew from) says so, but the
    program's exit is no failure."""
    manager, factory, trigger, clock = clocked_manager
    started = await spawn(manager, tmp_path)
    terminal_id = started.terminal_id
    adapter = factory.adapters[0]
    adapter.emit("partial output\r\n")
    await eventually(lambda: terminal_info(manager, terminal_id).screen_revision > 0)

    if failing == "read":
        error = "reader broke"

        def broken_read(_size: int) -> str:
            raise RuntimeError(error)

        # The read in progress returns the next output; the one after it fails.
        adapter.read = broken_read  # type: ignore[method-assign]
        adapter.emit("done")
        last_screen = "partial output\ndone"
    else:
        error = "renderer broke"

        async def broken_feed(_screen: TerminalScreen, _text: str) -> Any:
            raise RuntimeError(error)

        # Output is still read; only the screen falls behind it.
        monkeypatch.setattr(TerminalScreen, "feed", broken_feed)
        adapter.emit("lost")
        last_screen = "partial output"
    await eventually(
        lambda: any(
            record.levelno >= logging.WARNING and terminal_id in record.getMessage()
            for record in caplog.records
        )
    )

    # The program still runs, so it keeps its place and nothing reports its end.
    running = terminal_info(manager, terminal_id)
    assert running.state not in {"exited", "error"}
    assert running.finished_at is None
    with pytest.raises(ValueError, match="must be stopped"):
        manager.forget_for_operator(terminal_id)
    assert running.attention is None or running.attention.kind == "idle"

    adapter.finish(0)
    kind = "error" if failing == "read" else "exited"

    async def delivered() -> bool:
        # Without output to read, the program's end is noticed by a poll.
        await clock.advance(1)
        attention = terminal_info(manager, terminal_id).attention
        return (
            attention is not None
            and attention.kind == kind
            and any(kw["notice_id"] == attention.notice_id for _a, kw in trigger.submissions)
        )

    await eventually(delivered)
    body = trigger.submissions[-1][1]["body"]
    assert f"\n```text\n{last_screen}\n```" in body
    status = await call(
        manager, make_context(tmp_path), {"action": "status", "terminal_id": terminal_id}
    )
    if failing == "read":
        assert body.endswith(f"\n```text\n{last_screen}\n```")
        assert error in body
        assert status["data"]["state"] == "failed"
    else:
        behind = (
            f"This screen is out of date: drawing the program's output failed ({error}), so "
            "the screen lacks some of that output."
        )
        assert body.endswith(f"\n```\n{behind}")
        assert (status["data"]["state"], status["data"]["note"]) == ("exited", behind)


@pytest.mark.asyncio
async def test_renderer_recovery_does_not_back_off_beyond_five_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    factory = AdapterFactory()
    host = TerminalRenderHost.in_process()
    manager = TerminalManager(
        adapter_factory=factory,
        render_host=host,
        temporary_files=TemporaryFileManager(tmp_path / "data"),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        activity_quiet_seconds=1,
        idle_seconds=15,
    )
    manager.start()
    try:
        started = await spawn(manager, tmp_path)
        attempts: list[float] = []
        original_feed = TerminalScreen.feed
        original_open = host.open_screen

        async def broken_feed(_screen: TerminalScreen, _text: str) -> Any:
            raise RuntimeError("renderer unavailable")

        def broken_open(*_args: Any, **_kwargs: Any) -> TerminalScreen:
            attempts.append(clock.now)
            raise RuntimeError("renderer unavailable")

        monkeypatch.setattr(TerminalScreen, "feed", broken_feed)
        monkeypatch.setattr(host, "open_screen", broken_open)
        factory.adapters[0].emit("retained output")
        await eventually(lambda: len(attempts) == 1)
        for delay in (2, 4, 5, 5, 5, 5):
            count = len(attempts)
            await clock.advance(delay)
            await eventually(lambda count=count: len(attempts) > count)
        assert max(b - a for a, b in pairwise(attempts)) <= 5
        monkeypatch.setattr(TerminalScreen, "feed", original_feed)
        monkeypatch.setattr(host, "open_screen", original_open)
        await clock.advance(5)

        async def restored() -> bool:
            view = await manager.read_for_operator(started.terminal_id)
            return "retained output" in view["screen"]

        await eventually(restored)
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_a_stuck_final_redraw_does_not_delay_exit_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = AdapterFactory()
    trigger = PendingTriggerService()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        temporary_files=TemporaryFileManager(tmp_path / "data"),
    )
    manager.start()
    try:
        started = await spawn(manager, tmp_path)
        replay_started = asyncio.Event()

        async def broken_feed(_screen: TerminalScreen, _text: str) -> Any:
            raise RuntimeError("renderer unavailable")

        async def blocked_replay(_session: Any) -> bool:
            replay_started.set()
            await asyncio.Event().wait()
            return False

        monkeypatch.setattr(TerminalScreen, "feed", broken_feed)
        monkeypatch.setattr(terminal_output.TerminalOutput, "_redraw_once", blocked_replay)
        # Exercise the timeout branch without adding one second to the suite.
        assert terminal_output._FINAL_REDRAW_SECONDS <= 1
        monkeypatch.setattr(terminal_output, "_FINAL_REDRAW_SECONDS", 0.01)
        factory.adapters[0].emit("kept in the log")
        await replay_started.wait()
        factory.adapters[0].finish(0)
        await eventually(lambda: bool(trigger.submissions))
        assert terminal_info(manager, started.terminal_id).state == "exited"
    finally:
        await manager.aclose()
