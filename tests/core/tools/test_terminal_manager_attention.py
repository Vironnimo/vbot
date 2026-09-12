"""Terminal manager: attention behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.tools.terminal_manager import (
    TerminalManager,
    TerminalOwner,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    PendingTriggerService,
    eventually,
    owner,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import (
    terminal_manager as terminal_manager,
)


@pytest.mark.asyncio
async def test_operator_activity_wakes_the_attached_agent_session(tmp_path: Path) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        # A text-less Agent start suppresses the first settle (startup screen
        # is not work); operator input re-arms delivery.
        await manager.send_operator_input(session.terminal_id, "look\r")
        factory.adapters[0].emit("screen changed")
        await eventually(lambda: session.attention_revision == 1)

        assert session.state == "ready"
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
        await eventually(lambda: len(trigger.submissions) == 1)
        assert len(trigger.submissions) == 1
        assert trigger.submissions[0][0] == ("agent-a", "session-a")
    finally:
        await manager.aclose()


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
    tmp_path: Path,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data=None,
            text="do work",
            key="enter",
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
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
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["late output", "\x1b[1;1H"])
async def test_new_output_postpones_a_pending_agent_wakeup_to_the_next_quiet_boundary(
    tmp_path: Path,
    output: str,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data="begin",
            text=None,
            key=None,
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
        await eventually(lambda: len(trigger.submissions) == 1)

        factory.adapters[0].emit(output)
        await eventually(lambda: len(trigger.cancellations) == 1)
        await eventually(lambda: len(trigger.submissions) == 2)

        assert session.attention_revision == 2
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
        assert trigger.submissions[0][1]["notice_id"] != trigger.submissions[1][1]["notice_id"]
        assert trigger.submissions[1][1]["origin_run_id"] == "run-b"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_session_move_reroutes_pending_attention_to_new_owner(tmp_path: Path) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data=None,
            text="do work",
            key="enter",
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
        await eventually(lambda: len(trigger.submissions) == 1)
        target = TerminalOwner("project-b", "agent-b", "session-a")

        assert manager.transfer_scope(owner(), target) == 1
        await eventually(lambda: len(trigger.submissions) == 2)

        assert trigger.submissions[0][0] == ("agent-a", "session-a")
        assert trigger.submissions[1][0] == ("agent-b", "session-a")
        assert trigger.submissions[1][1]["project_id"] == "project-b"
        assert len(trigger.cancellations) == 1
        assert manager.get_session(session.terminal_id, target) is session
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_notification_revision_authorizes_only_the_delivered_screen(tmp_path):
    import re

    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(trigger, adapter_factory=factory, activity_quiet_seconds=0.03)
    manager.start()
    try:
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
        sent = await manager.send_input(
            session.terminal_id,
            owner(),
            data=None,
            text="y",
            key="enter",
            expected_screen_revision=revision,
            origin_run_id="answer",
        )
        assert sent["characters_sent"] == 2
        assert factory.adapters[0].writes[-2:] == ["y", "\r"]
        writes = list(factory.adapters[0].writes)
        with pytest.raises(TerminalStaleScreenError):
            await manager.send_input(
                session.terminal_id,
                owner(),
                data=None,
                text="y",
                key="enter",
                expected_screen_revision=revision,
                origin_run_id="duplicate",
            )
        assert factory.adapters[0].writes == writes
    finally:
        await manager.aclose()
