"""The Desktop owns native Computer Use and stops it without a server round trip."""

from __future__ import annotations

import asyncio
import threading
from typing import override

import pytest

from desktop.computer_execution import ComputerExecution
from resources.extensions.computer_use.target import InputInterrupted, TargetError
from tests.resources.extensions.computer_use.computer_use_test_support import FakeHotkey, FakeTarget

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "failure", ["cancel", "cancel_twice", "start_error", "disconnect", "shutdown"]
)
async def test_unfinished_native_hotkey_start_is_closed_before_control_returns(
    failure: str,
) -> None:
    target = FakeTarget()
    hotkeys: list[FakeHotkey] = []
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    class DelayedHotkey(FakeHotkey):
        @override
        def start(self) -> None:
            super().start()
            if len(hotkeys) != 1:
                return
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(1)
            if failure == "start_error":
                raise OSError("test hook setup failure")

    def hotkey(callback):
        result = DelayedHotkey(callback)
        hotkeys.append(result)
        return result

    computer = ComputerExecution(
        lambda *_: None, target_factory=lambda: target, hotkey_factory=hotkey
    )
    call = asyncio.create_task(computer.execute("control", {"control_id": "ctl_first"}))
    stopping = None
    try:
        await entered.wait()
        if failure in {"cancel", "cancel_twice"}:
            call.cancel()
        elif failure in {"disconnect", "shutdown"}:
            stopping = asyncio.create_task(
                computer.close() if failure == "shutdown" else computer.disconnected()
            )
        await asyncio.sleep(0)
        if failure == "cancel_twice":
            call.cancel()
            await asyncio.sleep(0)
        assert not call.done()
    finally:
        release.set()
    try:
        expected = {
            "cancel": asyncio.CancelledError,
            "cancel_twice": asyncio.CancelledError,
            "start_error": OSError,
            "disconnect": InputInterrupted,
            "shutdown": InputInterrupted,
        }[failure]
        with pytest.raises(expected):
            await call
        if stopping is not None:
            await stopping
        assert hotkeys[0].closed and hotkeys[0].armed is None
        assert target.inputs == []
        if failure != "shutdown":
            await computer.execute("control", {"control_id": "ctl_next"})
            await computer.execute("type", {"text": "new task"})
            assert target.inputs == [("type", "new task")]
            assert len(hotkeys) == 2 and not hotkeys[1].closed
    finally:
        await computer.close()


async def test_disconnect_interrupts_native_input_and_fences_queued_operations() -> None:
    target = FakeTarget()
    computer = ComputerExecution(
        lambda *_: None, target_factory=lambda: target, hotkey_factory=FakeHotkey
    )
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    def block(_record: tuple) -> None:
        loop.call_soon_threadsafe(entered.set)
        assert target.stop_event is not None
        assert target.stop_event.wait(5)

    target.on_input = block
    try:
        await computer.execute("control", {"control_id": "ctl_first"})
        typing = asyncio.create_task(computer.execute("type", {"text": "must stop"}))
        await entered.wait()
        queued = asyncio.create_task(computer.execute("move", {"x": 1, "y": 2}))
        await asyncio.sleep(0)
        await computer.disconnected()
        outcomes = await asyncio.gather(typing, queued, return_exceptions=True)
        assert isinstance(outcomes[0], InputInterrupted)
        assert isinstance(outcomes[1], TargetError)
        assert outcomes[1].code == "computer_offline"
        assert target.inputs == []
        assert target.released > 0 and target.activity[-1] is False
    finally:
        await computer.close()
    assert target.closed


async def test_cancelled_native_close_finishes_resource_cleanup() -> None:
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    class DelayedTarget(FakeTarget):
        @override
        def release_all(self) -> None:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(1)
            super().release_all()

    target = DelayedTarget()
    hotkeys: list[FakeHotkey] = []

    def hotkey(callback):
        result = FakeHotkey(callback)
        hotkeys.append(result)
        return result

    computer = ComputerExecution(
        lambda *_: None, target_factory=lambda: target, hotkey_factory=hotkey
    )
    await computer.execute("control", {"control_id": "ctl_first"})
    closing = asyncio.create_task(computer.close())
    try:
        await entered.wait()
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await computer.close()
    assert target.closed and hotkeys[0].closed
    assert target.released > 0 and target.activity[-1] is False


async def test_native_stop_blocks_the_old_control_without_waiting_for_the_server() -> None:
    target = FakeTarget()
    notifications: list[tuple] = []
    hotkeys: list[FakeHotkey] = []

    def hotkey(callback):
        result = FakeHotkey(callback)
        hotkeys.append(result)
        return result

    computer = ComputerExecution(
        lambda *args: notifications.append(args),
        target_factory=lambda: target,
        hotkey_factory=hotkey,
    )
    try:
        await computer.execute("control", {"control_id": "ctl_first"})
        await computer.execute("button", {"button": "left", "down": True})
        hotkeys[0].press_double_escape()
        assert notifications == [
            ("stopped", {"control_id": "ctl_first", "source": "double_escape"})
        ]
        with pytest.raises(InputInterrupted):
            await computer.execute("type", {"text": "must not arrive"})
        with pytest.raises(InputInterrupted):
            await computer.execute("control", {"control_id": "ctl_first"})
        await computer.execute("control", {"control_id": "ctl_next"})
        await computer.execute("type", {"text": "new task"})
        assert target.inputs[-1] == ("type", "new task")
        assert target.released > 0
    finally:
        await computer.close()
