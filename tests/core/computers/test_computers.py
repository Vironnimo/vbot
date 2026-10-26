"""Computer identity, Session selection and uncertain network effects."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.computers import ComputerError, ComputerRegistry, normalize_execution
from core.computers._store import COMPUTERS_PATH, validate_computers_file
from core.permissions import PermissionBoundary
from core.tools.tools import ToolContext


def hello(computer: str = "pc_laptop", **values: Any) -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol": 1,
        "computer_id": computer,
        "key": "computer-test-secret-which-is-never-returned",
        "instance_id": "inst_one",
        "name": "Laptop",
        "platform": "win32",
        "capabilities": ["terminal", "computer_use"],
        **values,
    }


def context(path: Path, **policy: Any) -> ToolContext:
    return ToolContext(
        agent_id="agent",
        session_id="session",
        workspace=path,
        vbot_root=path,
        data_root=path,
        run_id="run_test",
        tool_call_id="call_test",
        tool_name="terminal",
        tool_call_index=0,
        permissions=PermissionBoundary.UNRESTRICTED,
        execution=normalize_execution(policy or None),
    )


def test_policy_preserves_a_narrowed_selection_and_never_defaults_invalid_input() -> None:
    first = normalize_execution(None)
    first["allowed_computer_ids"].clear()
    assert normalize_execution(None)["allowed_computer_ids"] == ["server"]
    assert normalize_execution(
        {"computer_id": "pc_old", "allowed_computer_ids": [], "future": 1}
    ) == {
        "computer_id": "pc_old",
        "allowed_computer_ids": [],
    }
    for invalid in (
        "server",
        {},
        {"computer_id": ""},
        {"computer_id": "server", "allowed_computer_ids": "server"},
    ):
        with pytest.raises(ValueError):
            normalize_execution(invalid)


@pytest.mark.asyncio
async def test_selection_requires_exact_identity_grant_capability_and_connection(
    tmp_path: Path,
) -> None:
    registry = ComputerRegistry()
    connection = await registry.connect(hello(), actor="host")
    selected = context(tmp_path, computer_id="pc_laptop", allowed_computer_ids=["pc_laptop"])
    assert registry.resolve(selected, None, "terminal").id == "pc_laptop"
    assert registry.resolve(selected, "Laptop", "computer_use").id == "pc_laptop"
    for value, code in (("server", "computer_not_allowed"), ("Laptp", "computer_not_found")):
        with pytest.raises(ComputerError) as refused:
            registry.resolve(selected, value, "terminal")
        assert refused.value.code == code
    with pytest.raises(ComputerError) as refused:
        registry.resolve(selected, None, "files")
    assert refused.value.code == "computer_capability_unavailable"
    other = await registry.connect(hello("pc_other"), actor="host")
    with pytest.raises(ComputerError) as refused:
        registry.resolve(selected, "Laptop", "terminal")
    assert refused.value.code == "computer_not_found"
    registry.disconnect(connection)
    with pytest.raises(ComputerError) as refused:
        registry.resolve(selected, None, "terminal")
    assert refused.value.code == "computer_offline"
    registry.disconnect(other)
    registry.close()


@pytest.mark.asyncio
async def test_registration_ownership_survives_restart_and_preserves_future_fields(
    tmp_path: Path,
) -> None:
    registry = ComputerRegistry(tmp_path)
    await registry.connect(hello(), actor="dev_owner")
    registry.close()
    path = tmp_path / COMPUTERS_PATH
    saved = json.loads(path.read_text())
    assert hello()["key"] not in path.read_text()
    saved["future_root"] = 1
    saved["computers"][0]["future_entry"] = 2
    path.write_text(json.dumps(saved))
    restarted = ComputerRegistry(tmp_path)
    assert not restarted.get("pc_laptop").online
    for candidate, actor in (
        (hello(key="different-secret-with-at-least-32-characters"), "dev_owner"),
        (hello(), "dev_other"),
    ):
        with pytest.raises(ComputerError) as refused:
            await restarted.connect(candidate, actor=actor)
        assert refused.value.code == "computer_identity_conflict"
    await restarted.connect(hello(name="Renamed"), actor="dev_owner")
    rewritten = json.loads(path.read_text())
    assert rewritten["future_root"] == 1
    assert rewritten["computers"][0]["future_entry"] == 2
    assert validate_computers_file(path).ok
    assert "key_hash" not in json.dumps([item.describe() for item in restarted.list()])
    restarted.close()
    original = '{"format_version": 1, "computers": [{"id": "pc_broken"}]}'
    path.write_text(original)
    unavailable = ComputerRegistry(tmp_path)
    with pytest.raises(ComputerError) as refused:
        await unavailable.connect(hello(), actor="dev_owner")
    assert refused.value.code == "computer_store_unavailable"
    assert path.read_text() == original
    unavailable.close()


@pytest.mark.asyncio
async def test_calls_are_correlated_to_their_connection_and_never_replayed() -> None:
    registry = ComputerRegistry()
    connection = await registry.connect(hello(), actor="host")
    other = await registry.connect(hello("pc_other", name="Other"), actor="host")
    call = asyncio.create_task(registry.call("pc_laptop", "terminal", "write", {"text": "test"}))
    sent = await connection.outgoing.get()
    assert sent is not None
    registry.receive(
        other, {"type": "result", "id": sent["id"], "ok": True, "result": {"forged": True}}
    )
    assert not call.done()
    registry.receive(
        connection, {"type": "result", "id": sent["id"], "ok": True, "result": {"written": True}}
    )
    assert await call == {"written": True}
    pending = asyncio.create_task(
        registry.call("pc_laptop", "terminal", "write", {"text": "second"})
    )
    assert await connection.outgoing.get() is not None
    registry.disconnect(connection)
    with pytest.raises(ComputerError) as refused:
        await pending
    assert refused.value.code == "computer_connection_lost"
    reconnected = await registry.connect(hello(), actor="host")
    assert reconnected.outgoing.empty()
    await registry.wait_online("pc_laptop", instance_id="inst_one")
    restarted = await registry.connect(hello(instance_id="inst_two"), actor="host")
    with pytest.raises(ComputerError) as refused:
        await registry.call("pc_laptop", "terminal", "write", {}, instance_id="inst_one")
    assert refused.value.code == "computer_instance_changed"
    assert restarted.outgoing.empty()
    registry.close()


@pytest.mark.asyncio
async def test_same_instance_reconnect_wakes_readers_and_close_releases_waiters() -> None:
    registry = ComputerRegistry()
    connection = await registry.connect(hello(), actor="host")
    registry.disconnect(connection)
    waiter = asyncio.create_task(registry.wait_online("pc_laptop", instance_id="inst_one"))
    reconnected = await registry.connect(hello(), actor="host")
    await waiter
    registry.disconnect(reconnected)
    waiter = asyncio.create_task(registry.wait_online("pc_laptop", instance_id="inst_one"))
    registry.close()
    with pytest.raises(ComputerError) as refused:
        await waiter
    assert refused.value.code == "computers_closed"


@pytest.mark.asyncio
async def test_cancellation_and_timeout_drop_pending_calls_without_replaying() -> None:
    registry = ComputerRegistry()
    connection = await registry.connect(hello(), actor="host")
    call = asyncio.create_task(registry.call("pc_laptop", "terminal", "read", {}))
    request = await connection.outgoing.get()
    assert request is not None
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not connection.pending
    with pytest.raises(ComputerError) as refused:
        await registry.call("pc_laptop", "terminal", "status", {}, timeout=0)
    assert refused.value.code == "computer_timeout"
    assert not connection.pending
    registry.close()
