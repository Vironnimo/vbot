"""Computer use: cancellation behavior."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from resources.extensions.computer_use import extension as computer_use
from resources.extensions.computer_use.driver import ComputerUseError
from tests.resources.extensions.computer_use_helpers import (
    DesktopClient,
    call,
    capture,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


def test_transport_timeout_never_replays_uncertain_input():
    from resources.extensions.computer_use.driver import CuaDriver

    client = CuaDriver("test-owned-driver")
    client.desktop = None
    calls = []

    class Portal:
        def call(self, method, name, arguments):
            calls.append(name)
            if name == "start_session":
                return SimpleNamespace(
                    model_dump=lambda **kwargs: {"structuredContent": {"revived": False}}
                )
            raise TimeoutError()

    client._portal = Portal()
    client._session = SimpleNamespace(call_tool=None)
    client.schemas = {"click": {"properties": {"target": {}}}}
    with pytest.raises(ComputerUseError):
        client.call("click", {"session": "test-owned"})
    assert calls == ["start_session", "click"] and client.broken and client._session is None


@pytest.mark.parametrize("source", ["tool_cancel", "control", "double_escape"])
def test_interrupt_only_affects_current_call_and_next_agents_can_use_tool(
    computer, monkeypatch, source
):

    service, context, client, _ = computer
    service.executable = "test-owned-driver"
    service._driver = client
    monkeypatch.setattr(
        service, "_client", computer_use.ComputerUseService._client.__get__(service)
    )
    replacements = []

    def new_client(executable):
        replacement = DesktopClient()
        replacements.append(replacement)
        return replacement

    monkeypatch.setattr(computer_use, "CuaDriver", new_client)
    callbacks = []
    context = replace(context, cancel_registration_hook=callbacks.append)
    entered = threading.Event()
    stopped = threading.Event()
    original = client.interrupt

    def interrupt():
        original()
        stopped.set()

    client.interrupt = interrupt

    def block(name):
        if name == "list_apps":
            entered.set()
            assert stopped.wait(3)
            raise ComputerUseError("test-owned interrupted input")

    client.hook = block
    with ThreadPoolExecutor() as pool:
        future = pool.submit(service.handle, context, {"action": "apps"})
        assert entered.wait(3)
        if source == "tool_cancel":
            callbacks[0]()
        elif source == "control":
            status = asyncio.run(service.control({}))
            asyncio.run(service.control({"action": "stop", "call_id": status["call_id"]}))
        else:
            service._hotkey._key_event(0x1B, True, 0)
            service._hotkey._key_event(0x1B, False, 0)
            service._hotkey._key_event(0x1B, True, 0)
            owner = service._hotkey.pending_owner
            assert owner is service._active
            service._hotkey.callback(owner)
        result = future.result(timeout=2)
        assert result["error"]["code"] == "computer_use_interrupted"
    assert client.broken and client.closed and service._driver is None
    assert not (context.data_root / "computer-use-stopped").exists()
    assert not asyncio.run(service.control({}))["active"]
    assert service.handle(context, {"action": "apps"})["ok"]
    other = replace(context, agent_id="other", session_id="other-session", run_id="other-run")
    assert service.handle(other, {"action": "apps"})["ok"]
    assert len(replacements) == 1
    assert not replacements[0].broken


def test_late_cancel_does_not_stop_next_call(computer):
    service, context, client, _ = computer
    callbacks = []
    context = replace(context, cancel_registration_hook=callbacks.append)
    assert service.handle(context, {"action": "apps"})["ok"]
    client.hook = lambda name: callbacks[0]()
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not asyncio.run(service.control({}))["stopping"]
    assert not client.broken


def test_old_stop_file_has_no_runtime_effect_after_reload(computer, monkeypatch):
    service, context, _, _ = computer
    marker = context.data_root / "computer-use-stopped"
    marker.touch()
    replacement = computer_use.ComputerUseService(service.api)
    replacement.executable = "test-owned-driver"
    monkeypatch.setattr(computer_use, "CuaDriver", lambda executable: DesktopClient())
    asyncio.run(replacement.start(service.host))
    try:
        assert replacement.handle(context, {"action": "apps"})["ok"]
        assert set(asyncio.run(replacement.control({}))) == {
            "available",
            "active",
            "stopping",
            "hotkey_available",
        }
    finally:
        replacement.close()


def test_control_rejects_release_and_untargeted_stop(computer):
    service, context, client, _ = computer
    for arguments in ({"action": "resume"}, {"action": "stop"}):
        with pytest.raises(ValueError):
            asyncio.run(service.api.operations.invoke("control", arguments))
    # A stop after the targeted call completed is a no-op, not an idle lock.
    asyncio.run(service.control({"action": "stop", "call_id": "test-owned-expired-call"}))
    assert not client.broken
    assert service.handle(context, {"action": "apps"})["ok"]


def test_stale_control_request_does_not_cancel_next_call(computer):
    service, context, client, _ = computer
    status = []
    client.hook = lambda name: status.append(asyncio.run(service.control({})))
    assert service.handle(context, {"action": "apps"})["ok"]
    old = status[-1]
    client.hook = lambda name: asyncio.run(
        service.control({"action": "stop", "call_id": old["call_id"]})
    )
    # Provider call ids can be reused; the control reference still changes.
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not client.broken


def test_os_interrupt_failure_stops_remaining_work_without_latching(computer):
    service, context, client, _ = computer

    def denied():
        raise PermissionError("test-owned termination failure")

    client.interrupt = denied
    service._driver = client
    client.hook = lambda name: service.stop()
    assert (
        service.handle(context, {"action": "apps"})["error"]["code"] == "computer_use_interrupted"
    )
    assert client.broken and client.closed
    client.hook = None
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not (context.data_root / "computer-use-stopped").exists()


def test_stop_during_connection_admission_cannot_mark_driver_healthy(monkeypatch):
    from contextlib import asynccontextmanager

    from resources.extensions.computer_use.driver import CuaDriver

    closed = []

    @asynccontextmanager
    async def connection(self):
        self.interrupt()
        try:
            yield SimpleNamespace()
        finally:
            closed.append(True)

    monkeypatch.setattr(CuaDriver, "_connection", connection)
    client = CuaDriver("test-owned-driver")
    client.desktop = None
    with pytest.raises(ComputerUseError) as failure:
        client.connect()
    assert failure.value.code == "computer_use_interrupted"
    assert client.broken and client._session is None and closed == [True]


def test_real_owned_process_is_killed_without_waiting_for_rpc(tmp_path, monkeypatch):
    import sys
    import time

    from resources.extensions.computer_use import driver

    entered = threading.Event()
    real_open = driver.anyio.open_process
    processes = []
    script = """
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "test-owned", "version": "0.23.2"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "click", "inputSchema": {
            "type": "object", "properties": {"target": {}}}}]}
    elif request["params"]["name"] == "get_config":
        result = {"content": [], "structuredContent": {"max_image_dimension": 0}}
    else:
        time.sleep(120)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""

    async def launch(command, **kwargs):
        assert command == ["test-owned-driver", "mcp", "--direct"]
        process = await real_open([sys.executable, "-u", "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(driver.anyio, "open_process", launch)
    client = driver.CuaDriver("test-owned-driver")
    client.desktop = None
    client.connect()

    def call():
        entered.set()
        with pytest.raises(ComputerUseError):
            client.call("click", {})

    try:
        with ThreadPoolExecutor() as pool:
            result = pool.submit(call)
            assert entered.wait(3)
            start = time.monotonic()
            client.interrupt()
            result.result(timeout=3)
            assert time.monotonic() - start < 3
        assert processes[0].returncode is not None
        assert len(processes) == 1
        with pytest.raises(ComputerUseError):
            client.connect()
    finally:
        client.interrupt()
        client.close()


def test_idle_stop_and_escape_do_not_affect_later_calls(computer):
    service, context, client, _ = computer
    capture(computer)
    assert not asyncio.run(service.control({}))["active"]
    service.stop()
    for _ in range(2):
        service._hotkey._key_event(0x1B, True, 0)
        service._hotkey._key_event(0x1B, False, 0)
    assert service._hotkey.pending_owner is None
    assert not client.broken
    service.run_end(context)
    assert service.handle(replace(context, run_id="new-run"), {"action": "apps"})["ok"]


@pytest.mark.parametrize("capture_after", [False, True])
def test_pending_escape_stops_sequence_but_not_next_call(computer, capture_after):
    service, context, client, _ = computer
    capture(computer)

    def escape(name):
        if name == "type_text":
            service._hotkey._key_event(0x1B, True, 0)
            service._hotkey._key_event(0x1B, False, 0)
            service._hotkey._key_event(0x1B, True, 0)

    client.hook = escape
    result = call(
        computer,
        "sequence",
        capture_after=capture_after,
        steps=[
            {"action": "type", "text": "first"},
            {"action": "type", "text": "must not reach application"},
        ],
    )
    data = result["data"]
    assert data["completed_steps"] == 1 and data["stopped_step"] == 2
    assert data["partial"] and data["error"]["code"] == "computer_use_interrupted"
    assert data["observation_error"]["code"] == "computer_use_interrupted"
    assert sum(name == "type_text" for name, _ in client.calls) == 1
    # Even an undrained hotkey notification belongs only to the previous call.
    owner = service._hotkey.pending_owner
    assert owner is not None
    client.hook = lambda name: service._hotkey.callback(owner)
    assert service.handle(context, {"action": "apps"})["ok"]
    assert not client.broken


@pytest.mark.parametrize("capture_after", [False, True])
def test_interrupted_single_input_keeps_effect_and_notifies_agent(computer, capture_after):
    service, _, client, _ = computer
    capture(computer)
    client.hook = lambda name: service.stop() if name == "type_text" else None
    result = call(computer, "type", text="draft", capture_after=capture_after)
    assert result["ok"] and result["data"]["applied"]
    error = result["data"]["observation_error" if capture_after else "error"]
    assert error["code"] == "computer_use_interrupted"
    assert sum(name == "type_text" for name, _ in client.calls) == 1
    client.hook = None
    assert call(computer, "capture")["ok"]


def test_run_completion_and_shutdown_never_persist_a_stop(computer):
    service, context, _, _ = computer
    capture(computer)
    service.run_end(context)
    assert not service._sessions
    service.close()
    assert not (context.data_root / "computer-use-stopped").exists()
