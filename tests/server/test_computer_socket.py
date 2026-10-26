"""Native computer link admission, request ordering and reconnect semantics."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from core.computers import ComputerError, ComputerRegistry
from server._computer_socket import serve_computer_socket
from server.access import Principal
from server.access._principal import HOST, PRINCIPAL_SCOPE_KEY


class _EventBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))


class _Socket:
    def __init__(self, *, principal: Principal | None = HOST, origin: str | None = None) -> None:
        self.scope: dict[str, Any] = {PRINCIPAL_SCOPE_KEY: principal}
        self.headers = {} if origin is None else {"origin": origin}
        self.event_bus = _EventBus()
        self.app = SimpleNamespace(state=SimpleNamespace(event_bus=self.event_bus))
        self.accepted = False
        self.close_codes: list[int] = []
        self.incoming: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.send_started: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.call_send_gate = asyncio.Event()
        self.call_send_gate.set()

    def receive(self, value: Any) -> None:
        self.incoming.put_nowait(json.dumps(value))

    async def receive_text(self) -> str:
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def send_json(self, value: dict[str, Any]) -> None:
        if value.get("type") == "call":
            self.send_started.put_nowait(value)
            await self.call_send_gate.wait()
        transmitted = json.loads(json.dumps(value))
        self.sent.append(transmitted)
        self.outgoing.put_nowait(transmitted)

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int, reason: str = "") -> None:
        self.close_codes.append(code)


def _hello(computer_id: str = "pc_laptop", **changes: Any) -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol": 1,
        "computer_id": computer_id,
        "key": "test-registration-key-with-at-least-32-characters",
        "instance_id": "inst_running_app",
        "name": "Laptop",
        "platform": "win32",
        "capabilities": ["terminal", "computer_use"],
        **changes,
    }


async def _output(socket: _Socket, serving: asyncio.Task[None]) -> dict[str, Any]:
    """Fail promptly if the server ended instead of producing the next message."""
    receive = asyncio.create_task(socket.outgoing.get())
    try:
        done, _ = await asyncio.wait((receive, serving), return_when=asyncio.FIRST_COMPLETED)
        if receive in done:
            return receive.result()
        await serving
        pytest.fail("Computer connection ended before the expected message")
    finally:
        receive.cancel()
        with suppress(asyncio.CancelledError):
            await receive


@asynccontextmanager
async def _connected(
    registry: ComputerRegistry, computer_id: str = "pc_laptop"
) -> AsyncIterator[tuple[_Socket, asyncio.Task[None]]]:
    socket = _Socket()
    socket.receive(_hello(computer_id))
    serving = asyncio.create_task(serve_computer_socket(socket, registry))
    try:
        welcome = await _output(socket, serving)
        assert welcome == {
            "type": "welcome",
            "protocol": 1,
            "computer_id": computer_id,
            "instance_id": "inst_running_app",
            "server_instance": registry.server_instance,
        }
        yield socket, serving
    finally:
        if not serving.done():
            serving.cancel()
        with suppress(asyncio.CancelledError, WebSocketDisconnect):
            await serving


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "origin"),
    [(None, None), (HOST, "https://vbot.example"), (HOST, "null")],
)
async def test_only_a_native_admitted_client_can_register_a_computer(
    principal: Principal | None, origin: str | None
) -> None:
    registry = ComputerRegistry()
    socket = _Socket(principal=principal, origin=origin)
    socket.receive(_hello())
    try:
        await serve_computer_socket(socket, registry)
        assert not socket.accepted
        assert socket.close_codes == [4403]
        assert [computer.id for computer in registry.list()] == ["server"]
    finally:
        registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("registration", [[], _hello(protocol=2), _hello(capabilities="terminal")])
async def test_invalid_registration_is_refused_before_the_computer_becomes_available(
    registration: Any,
) -> None:
    registry = ComputerRegistry()
    socket = _Socket()
    socket.receive(registration)
    try:
        await serve_computer_socket(socket, registry)
        assert socket.accepted
        assert socket.close_codes == [1008]
        assert socket.sent[0]["type"] == "error"
        assert socket.sent[0]["error"]["code"] == "computer_protocol"
        assert [computer.id for computer in registry.list()] == ["server"]
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_link_correlates_replies_to_the_computer_that_received_the_request() -> None:
    registry = ComputerRegistry()
    processed = asyncio.Event()
    unsubscribe = registry.subscribe(
        lambda computer, event, data: (
            processed.set() if computer == "pc_other" and event == "stopped" else None
        )
    )
    try:
        async with (
            _connected(registry) as (socket, serving),
            _connected(registry, "pc_other") as (other, _),
        ):
            call = asyncio.create_task(
                registry.call("pc_laptop", "terminal", "write", {"text": "task"})
            )
            request = await _output(socket, serving)
            assert request["type"] == "call"
            assert request["capability"] == "terminal"
            assert request["operation"] == "write"
            assert request["params"] == {"text": "task"}
            other.receive(
                {"type": "result", "id": request["id"], "ok": True, "result": {"wrong": True}}
            )
            other.receive(
                {"type": "event", "capability": "computer_use", "event": "stopped", "data": {}}
            )
            await processed.wait()
            assert not call.done()
            socket.receive(
                {"type": "result", "id": request["id"], "ok": True, "result": {"written": True}}
            )
            assert await call == {"written": True}
            assert ("resource_changed", {"kind": "computers"}) in socket.event_bus.events
    finally:
        unsubscribe()
        registry.close()


@pytest.mark.asyncio
async def test_disconnect_marks_computer_offline_and_does_not_replay_uncertain_input() -> None:
    registry = ComputerRegistry()
    try:
        async with _connected(registry) as (socket, serving):
            call = asyncio.create_task(
                registry.call("pc_laptop", "terminal", "write", {"text": "one input"})
            )
            request = await _output(socket, serving)
            socket.incoming.put_nowait(WebSocketDisconnect(1006))
            with pytest.raises(WebSocketDisconnect):
                await serving
            with pytest.raises(ComputerError) as failure:
                await call
            assert failure.value.code == "computer_connection_lost"
            assert not registry.get("pc_laptop").online
            assert socket.event_bus.events == [
                ("resource_changed", {"kind": "computers"}),
                ("resource_changed", {"kind": "computers"}),
            ]
        async with _connected(registry) as (reconnected, serving):
            status = asyncio.create_task(
                registry.call("pc_laptop", "terminal", "status", {"process_id": "proc_existing"})
            )
            next_request = await _output(reconnected, serving)
            assert next_request["operation"] == "status"
            assert next_request["id"] != request["id"]
            reconnected.receive(
                {"type": "result", "id": next_request["id"], "ok": True, "result": {"alive": True}}
            )
            assert await status == {"alive": True}
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_cancelled_queued_request_is_not_sent_after_an_earlier_write_unblocks() -> None:
    registry = ComputerRegistry()
    try:
        async with _connected(registry) as (socket, serving):
            socket.call_send_gate.clear()
            first = asyncio.create_task(
                registry.call("pc_laptop", "terminal", "write", {"text": "first"})
            )
            await socket.send_started.get()
            admitted = asyncio.Event()

            async def second_call() -> dict[str, Any]:
                admitted.set()
                return await registry.call("pc_laptop", "terminal", "write", {"text": "cancelled"})

            second = asyncio.create_task(second_call())
            await admitted.wait()
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            socket.call_send_gate.set()
            first_request = await _output(socket, serving)
            socket.receive(
                {
                    "type": "result",
                    "id": first_request["id"],
                    "ok": True,
                    "result": {"written": True},
                }
            )
            assert await first == {"written": True}
            assert [item["params"] for item in socket.sent if item["type"] == "call"] == [
                {"text": "first"}
            ]
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_wrong_registration_key_cannot_replace_an_existing_computer_link() -> None:
    registry = ComputerRegistry()
    try:
        async with _connected(registry):
            attacker = _Socket()
            attacker.receive(_hello(key="another-registration-key-with-at-least-32-characters"))
            await serve_computer_socket(attacker, registry)
            assert attacker.close_codes == [4403]
            assert attacker.sent[0]["error"]["code"] == "computer_identity_conflict"
            assert registry.get("pc_laptop").online
    finally:
        registry.close()
