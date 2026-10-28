"""Computer Use through the real computer broker and an in-memory native Desktop."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from core.computers import ComputerRegistry
from core.computers.computers import ComputerConnection
from desktop.computer_execution import ComputerExecution
from tests.resources.extensions.computer_use.computer_use_test_support import (
    FakeHotkey,
    FakeTarget,
    Harness,
    images,
)

pytestmark = pytest.mark.asyncio


class NativeLink:
    def __init__(self, registry: ComputerRegistry, *, hotkey_available: bool = True) -> None:
        self.registry = registry
        self.target = FakeTarget()

        def hotkey(callback):
            result = FakeHotkey(callback)
            result.available = hotkey_available
            return result

        self.native = ComputerExecution(
            self._emit, target_factory=lambda: self.target, hotkey_factory=hotkey
        )
        self.connection: ComputerConnection | None = None
        self.pump: asyncio.Task | None = None
        self.answers: set[asyncio.Task] = set()

    async def connect(self) -> None:
        self.connection = await self.registry.connect(
            {
                "type": "hello",
                "protocol": 1,
                "computer_id": "pc_laptop",
                "instance_id": "app_running",
                "name": "Laptop",
                "platform": "win32",
                "key": "test-computer-key-0000000000000000",
                "capabilities": ["computer_use"],
            },
            actor="test",
        )
        self.pump = asyncio.create_task(self._pump(self.connection))

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        assert self.connection is not None
        self.registry.receive(
            self.connection,
            {"type": "event", "capability": "computer_use", "event": event, "data": data},
        )

    async def _pump(self, connection: ComputerConnection) -> None:
        while (request := await connection.outgoing.get()) is not None:
            task = asyncio.create_task(self._answer(connection, json.loads(json.dumps(request))))
            self.answers.add(task)
            task.add_done_callback(self.answers.discard)

    async def _answer(self, connection: ComputerConnection, request: dict[str, Any]) -> None:
        try:
            result = await self.native.execute(request["operation"], request["params"])
            response = {"type": "result", "id": request["id"], "ok": True, "result": result}
        except Exception as error:
            response = {
                "type": "result",
                "id": request["id"],
                "ok": False,
                "error": {"code": getattr(error, "code", "unexpected"), "message": str(error)},
            }
        self.registry.receive(connection, json.loads(json.dumps(response)))

    async def disconnect(self) -> None:
        assert self.connection is not None
        self.registry.disconnect(self.connection)
        await self.native.disconnected()
        if self.pump is not None:
            await self.pump
        if self.answers:
            await asyncio.gather(*tuple(self.answers))

    async def close(self, computer: Harness) -> None:
        assert computer.service._routing is not None
        await computer.service._routing.close()
        await self.disconnect()
        await self.native.close()


def remote_context(computer: Harness, **changes: Any):
    return computer.context_for(
        "computer",
        execution={"computer_id": "pc_laptop", "allowed_computer_ids": ["server", "pc_laptop"]},
        **changes,
    )


@pytest.mark.parametrize("hotkey_available", [True, False])
async def test_remote_calls_keep_policy_images_and_input_on_the_selected_computer(
    computer: Harness,
    hotkey_available: bool,
) -> None:
    link = NativeLink(computer.computers, hotkey_available=hotkey_available)
    await link.connect()
    try:
        server_context = computer.context_for("computer")
        assert (await computer.call("computer", {"action": "screenshot"}, server_context))["ok"]
        server_image = server_context.result_media[-1]["filename"].removesuffix(".png")
        remote = remote_context(computer)
        result = await computer.call("computer", {"action": "screenshot"}, remote)
        assert result["ok"] and images(remote)
        status = await computer.api.operations.invoke("control", {"action": "status"})
        assert status["computer_id"] == "pc_laptop"
        assert status["hotkey_available"] is hotkey_available
        remote_image = remote.result_media[-1]["filename"].removesuffix(".png")
        assert remote_image != server_image
        wrong = await computer.call(
            "computer",
            {"action": "left_click", "coordinate": [10, 10], "screenshot_id": server_image},
            remote_context(computer),
        )
        assert wrong["error"]["code"] == "invalid_arguments"
        assert link.target.inputs == computer.target.inputs == []
        result = await computer.call(
            "computer",
            {"action": "left_click", "coordinate": [10, 10], "screenshot_id": remote_image},
            remote_context(computer),
        )
        assert result["ok"]
        assert link.target.inputs[0][:3] == ("click", 110, 110)
        assert computer.target.inputs == []

        denied = await computer.call(
            "computer", {"action": "type", "text": "no", "computer_id": "Laptop"}
        )
        assert denied["error"]["code"] == "computer_not_allowed"
        assert len(link.target.inputs) == 1

        # A server with no native desktop still exposes the remote capability.
        computer.service._unready = "No interactive server desktop"
        assert computer.service.ready()
        assert (
            await computer.call("computer", {"action": "screenshot"}, remote_context(computer))
        )["ok"]
    finally:
        await link.close(computer)


async def test_reconnect_stops_the_old_run_and_invalidates_its_screen_references(
    computer: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = NativeLink(computer.computers)
    await link.connect()
    try:
        context = remote_context(computer)
        assert (await computer.call("computer", {"action": "screenshot"}, context))["ok"]
        screenshot_id = context.result_media[-1]["filename"].removesuffix(".png")
        assert computer.service._routing is not None
        service = computer.service._routing._remotes["pc_laptop"].service
        queued = asyncio.Event()
        active_call = service._active_call

        @asynccontextmanager
        async def observe_queue(context, desktop):
            queued.set()
            async with active_call(context, desktop):
                yield

        monkeypatch.setattr(service, "_active_call", observe_queue)
        await service._lock.acquire()
        pending = asyncio.create_task(
            computer.call(
                "computer",
                {"action": "type", "text": "must not survive reconnect"},
                remote_context(computer, run_id="queued_run"),
            )
        )
        try:
            await queued.wait()
            await link.disconnect()
        finally:
            # Hold admission until the same native instance has reconnected.
            await link.connect()
            service._lock.release()
        assert (await pending)["error"]["code"] == "computer_use_interrupted"
        await link.disconnect()
        offline = await computer.call(
            "computer", {"action": "type", "text": "no"}, remote_context(computer)
        )
        assert offline["error"]["code"] == "computer_offline"
        await link.connect()
        stopped = await computer.call(
            "computer", {"action": "type", "text": "no"}, remote_context(computer)
        )
        assert stopped["error"]["code"] == "computer_use_interrupted"
        stale = await computer.call(
            "computer",
            {"action": "left_click", "coordinate": [10, 10], "screenshot_id": screenshot_id},
            remote_context(computer, run_id="next_run"),
        )
        assert stale["error"]["code"] == "invalid_arguments"
        assert link.target.inputs == []
        fresh = await computer.call(
            "computer", {"action": "screenshot"}, remote_context(computer, run_id="next_run")
        )
        assert fresh["ok"]
        await link.disconnect()
        await computer.computers.forget("pc_laptop")
        routing = computer.service._routing
        await asyncio.gather(*tuple(routing._closing))
        assert "pc_laptop" not in routing._remotes
        assert service._closed
        assert service._executor is not None and service._executor._shutdown
    finally:
        await link.close(computer)
