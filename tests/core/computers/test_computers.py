"""Computer identity, Session selection and uncertain network effects."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, override

import pytest

from core.computers import ComputerError, ComputerRegistry, normalize_execution
from core.computers._store import COMPUTERS_PATH, MAX_COMPUTERS, validate_computers_file
from core.permissions import PermissionBoundary
from core.tools.tools import ToolContext
from core.utils import native_capabilities


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
@pytest.mark.parametrize(
    "capabilities",
    [
        ["extension:owner:device", "extension:owner:device"],
        ["extension:bad%2Fowner:device"],
        ["extension:owner:device"],
    ],
)
async def test_native_registration_rejects_malformed_or_oversized_capability_catalogs(
    capabilities: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_capabilities, "MAX_CAPABILITY_IDS_BYTES", 10)
    registry = ComputerRegistry()
    try:
        with pytest.raises(ComputerError) as refused:
            await registry.connect(hello(capabilities=capabilities), actor="host")
        assert refused.value.code == "computer_protocol"
        assert len(registry.list()) == 1
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_selection_requires_exact_identity_grant_capability_and_connection(
    tmp_path: Path,
) -> None:
    registry = ComputerRegistry()
    connection = await registry.connect(hello(), actor="host")
    computer_id = connection.info.id
    selected = context(tmp_path, computer_id=computer_id, allowed_computer_ids=[computer_id])
    assert registry.resolve(selected, None, "terminal").id == computer_id
    assert registry.resolve(selected, "Laptop", "computer_use").id == computer_id
    for value, code in (("server", "computer_not_allowed"), ("Laptp", "computer_not_found")):
        with pytest.raises(ComputerError) as refused:
            registry.resolve(selected, value, "terminal")
        assert refused.value.code == code
    with pytest.raises(ComputerError) as refused:
        registry.resolve(selected, None, "files")
    assert refused.value.code == "computer_capability_unavailable"
    registry.disconnect(connection)
    with pytest.raises(ComputerError) as refused:
        registry.resolve(selected, None, "terminal")
    assert refused.value.code == "computer_offline"
    await registry.forget(computer_id)
    replacement = await registry.connect(
        hello(computer_id, name=computer_id, key="b" * 40), actor="host"
    )
    assert replacement.info.id != computer_id
    with pytest.raises(ComputerError) as refused:
        registry.resolve(selected, computer_id, "terminal")
    assert refused.value.code == "computer_not_allowed"
    selected = context(
        tmp_path, computer_id=computer_id, allowed_computer_ids=[computer_id, replacement.info.id]
    )
    with pytest.raises(ComputerError) as refused:
        registry.resolve(selected, None, "terminal")
    assert refused.value.code == "computer_not_found"
    assert registry.resolve(selected, computer_id, "terminal").id == replacement.info.id
    registry.close()


@pytest.mark.asyncio
async def test_worker_can_resolve_a_name_during_registration_removal(tmp_path: Path) -> None:
    entered = threading.Event()
    resume = threading.Event()

    class PausedName(str):
        @override
        def casefold(self) -> str:
            entered.set()
            assert resume.wait(2)
            return super().casefold()

    registry = ComputerRegistry()
    laptop = await registry.connect(hello(name=PausedName("Laptop")), actor="host")
    other = await registry.connect(hello("pc_other", name="Workstation"), actor="host")
    registry.disconnect(other)
    selected = context(tmp_path, computer_id=laptop.info.id, allowed_computer_ids=[laptop.info.id])
    selecting = asyncio.create_task(
        asyncio.to_thread(registry.resolve, selected, "Laptop", "terminal")
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await registry.forget(other.info.id)
    finally:
        resume.set()
        info = await selecting
        registry.close()
    assert info.id == laptop.info.id


@pytest.mark.asyncio
async def test_registration_ownership_survives_restart_and_preserves_future_fields(
    tmp_path: Path,
) -> None:
    registry = ComputerRegistry(tmp_path)
    capabilities = ["terminal", *[f"extension:office:device_{number}" for number in range(64)]]
    first = await registry.connect(hello(capabilities=capabilities), actor="dev_owner")
    registry.close()
    path = tmp_path / COMPUTERS_PATH
    # A protocol-1 Desktop that never received its first welcome retries its original id.
    retry = ComputerRegistry(tmp_path)
    recovered = await retry.connect(hello(capabilities=capabilities), actor="dev_owner")
    assert recovered.info.id == first.info.id != hello()["computer_id"]
    assert len(retry.list()) == 2
    retry.close()
    saved = json.loads(path.read_text())
    assert hello()["key"] not in path.read_text()
    # The Generation 1 registration written before server-assigned ids keeps its identity.
    saved["computers"][0]["id"] = saved["computers"][0].pop("registration_id")
    saved["future_root"] = 1
    saved["computers"][0]["future_entry"] = 2
    path.write_text(json.dumps(saved))
    restarted = ComputerRegistry(tmp_path)
    assert restarted.get("pc_laptop").describe()["capabilities"] == capabilities
    assert not restarted.get("pc_laptop").online
    for candidate, actor, code in (
        (
            hello(key="different-secret-with-at-least-32-characters"),
            "dev_owner",
            "computer_identity_conflict",
        ),
        # The key proves the installation, but a connection without a sign-in
        # cannot take over one registered with a sign-in.
        (hello(), "open", "computer_sign_in_required"),
    ):
        with pytest.raises(ComputerError) as refused:
            await restarted.connect(candidate, actor=actor)
        assert refused.value.code == code
    # A changed sign-in with the installation's key takes the registration over.
    await restarted.connect(hello(), actor="host")
    assert json.loads(path.read_text())["computers"][0]["actor"] == "host"
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
async def test_registration_needs_a_free_name_and_room_and_offline_computers_can_be_removed(
    tmp_path: Path,
) -> None:
    registry = ComputerRegistry(tmp_path)
    laptop = await registry.connect(hello(), actor="host")
    for name, code in (
        ("SERVER", "computer_name_reserved"),
        ("  laptop ", "computer_name_taken"),
    ):
        with pytest.raises(ComputerError) as refused:
            await registry.connect(hello("pc_other", name=name), actor="host")
        assert refused.value.code == code
    for index in range(MAX_COMPUTERS - 1):
        await registry.connect(hello(f"pc_{index}", name=f"PC {index}"), actor="host")
    with pytest.raises(ComputerError) as refused:
        await registry.connect(hello("pc_extra", name="Extra"), actor="host")
    assert refused.value.code == "computer_limit_reached"
    # A registered computer still reconnects at the limit.
    laptop = await registry.connect(hello(), actor="host")

    for computer_id, code in (
        ("server", "computer_not_removable"),
        (laptop.info.id, "computer_online"),
        ("pc_missing", "computer_not_found"),
    ):
        with pytest.raises(ComputerError) as refused:
            await registry.forget(computer_id)
        assert refused.value.code == code
    registry.disconnect(laptop)
    await registry.forget(laptop.info.id)
    assert laptop.info.id not in json.dumps(json.loads((tmp_path / COMPUTERS_PATH).read_text()))
    with pytest.raises(ComputerError) as refused:
        registry.get(laptop.info.id)
    assert refused.value.code == "computer_not_found"
    # Its name and its place are free again.
    await registry.connect(hello("pc_extra", name="Laptop"), actor="host")
    registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["new", "existing", "shutdown"])
async def test_interrupted_registration_keeps_the_saved_identity_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    registry = ComputerRegistry(tmp_path)
    old = await registry.connect(hello(), actor="host") if case == "existing" else None
    events: list[tuple[str, str]] = []
    registry.subscribe(lambda computer_id, event, _data: events.append((computer_id, event)))
    assert registry._store is not None
    original_claim = registry._store.claim
    started = asyncio.get_running_loop().create_future()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def delayed_claim(*args: Any) -> str:
        loop.call_soon_threadsafe(started.set_result, None)
        assert release.wait(5)
        return original_claim(*args)

    monkeypatch.setattr(registry._store, "claim", delayed_claim)
    task = asyncio.create_task(registry.connect(hello(name="Renamed"), actor="host"))
    try:
        await asyncio.wait_for(started, 2)
        if case == "shutdown":
            registry.close()
        else:
            task.cancel("connection closed")
    finally:
        release.set()
    if case == "shutdown":
        with pytest.raises(ComputerError) as closed:
            await task
        assert closed.value.code == "computers_closed"
    else:
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert cancelled.value.args == ("connection closed",)
    _, info = registry.list()
    assert info.name == "Renamed"
    assert not info.online
    assert old is None or old.closed
    if case != "shutdown":
        assert (info.id, "disconnected") in events
    saved = json.loads((tmp_path / COMPUTERS_PATH).read_text())
    assert saved["computers"][0]["name"] == info.name
    if case != "shutdown":
        monkeypatch.setattr(registry._store, "claim", original_claim)
        connection = await registry.connect(hello(name="Renamed"), actor="host")
        assert connection.info.id == info.id
        assert registry.get(info.id).online
        registry.disconnect(connection)
    registry.close()


@pytest.mark.asyncio
async def test_cancelled_removal_publishes_the_saved_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ComputerRegistry(tmp_path)
    connection = await registry.connect(hello(), actor="host")
    registry.disconnect(connection)
    events: list[tuple[str, str]] = []
    registry.subscribe(lambda computer_id, event, _data: events.append((computer_id, event)))
    assert registry._store is not None
    original_forget = registry._store.forget
    started = asyncio.get_running_loop().create_future()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def delayed_forget(computer_id: str) -> None:
        loop.call_soon_threadsafe(started.set_result, None)
        assert release.wait(5)
        original_forget(computer_id)

    monkeypatch.setattr(registry._store, "forget", delayed_forget)
    task = asyncio.create_task(registry.forget(connection.info.id))
    try:
        await asyncio.wait_for(started, 2)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ComputerError) as missing:
        registry.get(connection.info.id)
    assert missing.value.code == "computer_not_found"
    assert (connection.info.id, "removed") in events
    assert json.loads((tmp_path / COMPUTERS_PATH).read_text())["computers"] == []
    monkeypatch.setattr(registry._store, "forget", original_forget)
    await registry.connect(hello(), actor="host")
    registry.close()


@pytest.mark.asyncio
async def test_calls_are_correlated_to_their_connection_and_never_replayed() -> None:
    registry = ComputerRegistry()
    connection = await registry.connect(hello(), actor="host")
    other = await registry.connect(hello("pc_other", name="Other"), actor="host")
    call = asyncio.create_task(
        registry.call(connection.info.id, "terminal", "write", {"text": "test"})
    )
    sent = await connection.outgoing.get()
    assert sent is not None
    registry.receive(
        connection,
        {"type": "capabilities", "capabilities": ["terminal", "extension:office:printer"]},
    )
    assert registry.get(connection.info.id).capabilities == ("terminal", "extension:office:printer")
    assert not connection.closed and registry.get(connection.info.id).instance_id == "inst_one"
    assert sent["id"] in connection.pending
    registry.receive(
        other, {"type": "result", "id": sent["id"], "ok": True, "result": {"forged": True}}
    )
    assert not call.done()
    registry.receive(
        connection, {"type": "result", "id": sent["id"], "ok": True, "result": {"written": True}}
    )
    assert await call == {"written": True}
    pending = asyncio.create_task(
        registry.call(connection.info.id, "terminal", "write", {"text": "second"})
    )
    assert await connection.outgoing.get() is not None
    registry.disconnect(connection)
    with pytest.raises(ComputerError) as refused:
        await pending
    assert refused.value.code == "computer_connection_lost"
    reconnected = await registry.connect(hello(), actor="host")
    assert reconnected.outgoing.empty()
    assert registry.get(connection.info.id).instance_id == "inst_one"
    restarted = await registry.connect(hello(instance_id="inst_two"), actor="host")
    with pytest.raises(ComputerError) as refused:
        await registry.call(connection.info.id, "terminal", "write", {}, instance_id="inst_one")
    assert refused.value.code == "computer_instance_changed"
    assert restarted.outgoing.empty()
    registry.close()


@pytest.mark.asyncio
async def test_cancellation_and_timeout_drop_pending_calls_without_replaying() -> None:
    registry = ComputerRegistry()
    connection = await registry.connect(hello(), actor="host")
    call = asyncio.create_task(registry.call(connection.info.id, "terminal", "read", {}))
    request = await connection.outgoing.get()
    assert request is not None
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not connection.pending
    with pytest.raises(ComputerError) as refused:
        await registry.call(connection.info.id, "terminal", "status", {}, timeout=0)
    assert refused.value.code == "computer_timeout"
    assert not connection.pending
    registry.close()
