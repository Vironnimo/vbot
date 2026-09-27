"""Extension-owned temporary Sessions: bindings, delivery receipts, owned Runs, groups."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.chat.messages import ToolCall
from core.runs import Run, RunExecutionOwner
from core.sessions import ChatSessionManager, SessionAddress, TemporarySessionBinding
from tests.core.sessions.history_fixtures import complete_run
from tests.core.sessions.sessions_test_support import _address


def _bind(
    sessions: ChatSessionManager,
) -> tuple[TemporarySessionBinding, RunExecutionOwner]:
    binding = sessions.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="fixture",
        group_id="group",
        participant_id="peer",
        config={},
    )
    owner = RunExecutionOwner("fixture", "group", "peer", binding.generation_id, "epoch")
    return binding, owner


async def _admit(sessions, address, run_id, owner, input_id=None):
    """Admit *run_id* with its execution owner, as the Run manager does."""
    await sessions.start_run(
        Run(
            run_id=run_id,
            agent_id=address.agent_id,
            session_id=address.session_id,
            project_id=address.project_id,
            execution_owner=owner,
            execution_input_id=input_id,
        )
    )


def _summary(run_id, status="completed"):
    return ChatMessage.run_summary(
        run_id=run_id,
        status=status,
        iteration_count=1,
        timing={
            "started_at": "2026-09-07T12:00:00+00:00",
            "completed_at": "2026-09-07T12:00:01+00:00",
            "duration_ms": 1000,
        },
    )


def _contents(sessions: ChatSessionManager, address: SessionAddress) -> list[object]:
    return [
        message.content for message in sessions.get(address).load() if message.role != "assistant"
    ]


def _announce(sessions: ChatSessionManager, address: SessionAddress, batch) -> None:
    """Append the Assistant turn whose Tool calls the delivered results answer."""
    sessions.get(address).append(
        ChatMessage.assistant(
            model="test",
            content=None,
            tool_calls=[
                ToolCall(id=message.tool_call_id, name=message.name)
                for message in batch
                if message.role == "tool" and message.tool_call_id and message.name
            ],
        )
    )


@pytest.mark.asyncio
async def test_temporary_binding_and_receipt_are_generation_scoped_and_idempotent(
    manager,
) -> None:
    address = _address("temporary", "participant")

    def bind():
        return manager.create_bound_temporary_session(
            address,
            owner_name="swarm",
            group_id="group",
            participant_id="participant",
            config={"model": "test/model"},
        )

    def deliver(messages, receipts, **options):
        manager.append_messages_with_receipts(
            address,
            generation_id=binding.generation_id,
            owner_name="swarm",
            messages=messages,
            receipts=receipts,
            **options,
        )

    async def receipt(delivery_id):
        return await manager.lookup_delivery_receipt(
            address, binding.generation_id, "swarm", delivery_id
        )

    binding = bind()
    assert bind() == binding

    for _ in range(2):
        deliver(
            [ChatMessage.note("delivery")],
            [(0, "delivery-1", "hash", "note", "note")],
            deduplicate_carrier=True,
        )
    assert _contents(manager, address) == ["delivery"]
    first = await receipt("delivery-1")
    assert first is not None
    assert first.carrier_location == {"kind": "note", "sequence": 0}

    tools_and_note = [
        ChatMessage.tool(tool_call_id="one", name="first", content="tool-one"),
        ChatMessage.tool(tool_call_id="two", name="second", content="tool-two"),
        ChatMessage.note("after-tools"),
    ]
    _announce(manager, address, tools_and_note)
    deliver(
        tools_and_note,
        [(0, "tool-1", "hash-1", "delivery", "tool"), (1, "tool-2", "hash-2", "delivery", "tool")],
    )
    assert _contents(manager, address) == ["delivery", "tool-one", "tool-two", "after-tools"]
    tool_two = await receipt("tool-2")
    assert tool_two is not None
    assert tool_two.carrier_location == {"kind": "tool", "sequence": 3}

    # A batch may repeat an earlier receipt beside a new one.
    replayed_batch = [
        ChatMessage.tool(tool_call_id="one-retry", name="first", content="tool-one-retry"),
        ChatMessage.tool(tool_call_id="three", name="third", content="tool-three"),
        ChatMessage.note("after-retry"),
    ]
    _announce(manager, address, replayed_batch)
    deliver(
        replayed_batch,
        [(0, "tool-1", "hash-1", "delivery", "tool"), (1, "tool-3", "hash-3", "delivery", "tool")],
    )
    delivered = [
        "delivery",
        "tool-one",
        "tool-two",
        "after-tools",
        "tool-one-retry",
        "tool-three",
        "after-retry",
    ]
    assert _contents(manager, address) == delivered
    tool_three = await receipt("tool-3")
    assert tool_three is not None
    assert tool_three.carrier_location == {"kind": "tool", "sequence": 7}

    # A conflicting receipt rolls back the whole delivery.
    with pytest.raises(ChatSessionError):
        deliver(
            [ChatMessage.note("must roll back"), ChatMessage.note("conflict")],
            [
                (0, "delivery-2", "hash-2", "note", "note"),
                (1, "delivery-1", "different", "note", "note"),
            ],
        )
    assert _contents(manager, address) == delivered
    assert await receipt("delivery-2") is None
    with pytest.raises(ChatSessionError, match="delivery receipt is invalid"):
        deliver(
            [ChatMessage.tool(tool_call_id="bad", name="bad", content="bad")],
            [(0, "bad", "hash", "delivery", "note")],
        )
    assert bind() == binding


@pytest.mark.asyncio
async def test_owner_managed_session_rejects_lifecycle_mutations_at_storage_boundary(
    manager,
) -> None:
    address = _address("temporary", "participant")
    binding = manager.create_bound_temporary_session(
        address,
        owner_name="swarm",
        group_id="group",
        participant_id="participant",
        config={},
    )
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        await manager.move(address, _address("ordinary", "moved"))
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        await manager.archive(address)
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        manager.delete(address)
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        manager.get(address).delete()
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        manager.archive_identity_agent_sessions("temporary")
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        manager.retarget_identity_agent_sessions("temporary", "ordinary")
    assert manager.temporary_binding(address) == binding
    # Ordinary metadata never makes it listable.
    manager.set_metadata(address, {"title": "ordinary metadata"})
    assert manager.list_summaries_page([(None, "temporary")], limit=20).sessions == ()


@pytest.mark.asyncio
async def test_owned_run_survives_reopen_and_requires_exact_durable_terminal(
    manager, tmp_path
) -> None:
    binding, owner = _bind(manager)
    child = manager.create("existing", "target")
    child.append(ChatMessage.user("older unrelated work"))
    complete_run(child.start_run("old"), _summary("old"))
    await _admit(manager, child.address, "owned", owner)
    owned = child.for_run("owned")
    owned.append(ChatMessage.user("owned work"))
    complete_run(child.start_run("foreign"), _summary("foreign"))
    rows = manager.owned_runs(owner_name="fixture", group_id="group")
    assert len(rows) == 1
    assert rows[0].owner == owner
    assert rows[0].address == child.address
    assert rows[0].start_sequence == 2
    assert rows[0].terminal_status is None
    assert manager.temporary_binding(child.address) is None
    # Admitting the running Run again with the same owner changes nothing.
    await _admit(manager, child.address, "owned", owner)
    assert manager.owned_runs(owner_name="fixture", group_id="group") == rows
    complete_run(owned, _summary("owned"))
    manager.close()

    reopened = ChatSessionManager(tmp_path)
    try:
        records = reopened.owned_runs(owner_name="fixture", group_id="group")
        assert records[0].start_sequence == 2
        assert records[0].terminal_status == "completed"
        assert records[0].terminal_sequence == 4
        assert records[0].owner.generation_id == binding.generation_id
        assert records[0].generation_id != binding.generation_id
        assert not reopened.owned_runs(owner_name="other", group_id="group")
        assert not reopened.owned_runs(owner_name="fixture", group_id="other")
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_owner_claim_cannot_rebind_and_forked_history_has_no_execution_owner(
    manager,
) -> None:
    binding, owner = _bind(manager)
    await _admit(manager, binding.address, "run", owner)
    with pytest.raises(ChatSessionError):
        await _admit(manager, binding.address, "run", replace(owner, epoch="other"))
    with pytest.raises(ChatSessionError):
        await _admit(manager, binding.address, "forged", replace(owner, generation_id="old"))
    session = manager.get(binding.address).for_run("run")
    session.append(ChatMessage.user("goal"))
    complete_run(session, _summary("run", "cancelled"))
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        await manager.fork(binding.address)
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        await manager.fork(binding.address, target_agent_id=binding.address.agent_id)
    fork = await manager.fork(binding.address, target_agent_id="ordinary")
    assert fork.load() == []
    assert [message.role for message in fork.load_active()] == ["user", "run_summary"]
    assert manager.temporary_binding(fork.address) is None
    records = manager.owned_runs(owner_name="fixture", group_id="group")
    assert len(records) == 1
    assert records[0].address == binding.address
    assert records[0].terminal_status == "cancelled"
    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        await manager.archive(binding.address)
    assert manager.owned_runs(owner_name="fixture", group_id="group") == records


@pytest.mark.asyncio
async def test_owner_pages_are_bounded_and_partition_participants(manager) -> None:
    binding, owner = _bind(manager)
    for number in range(3):
        await _admit(manager, binding.address, f"run{number}", owner)
    first = manager.owned_runs(owner_name="fixture", group_id="group", limit=2)
    second = manager.owned_runs(
        owner_name="fixture",
        group_id="group",
        after=first[-1].record_key,
        limit=2,
    )
    assert [row.run_id for row in first + second] == ["run0", "run1", "run2"]
    assert not manager.owned_runs(
        owner_name="fixture",
        group_id="group",
        participant_id="other",
    )
    for invalid in (True, 0, 1001, "20"):
        with pytest.raises(ValueError):
            manager.owned_runs(owner_name="fixture", group_id="group", limit=invalid)


@pytest.mark.asyncio
async def test_owned_run_point_lookups_resolve_exact_ids_and_inputs(manager, monkeypatch) -> None:
    from core.sessions import _store_owned

    # Ids beyond one bounded statement are read in batches of the same snapshot.
    monkeypatch.setattr(_store_owned, "_OWNED_RUN_LOOKUP_BATCH", 4)
    binding, owner = _bind(manager)
    other_group = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "other-participant"),
        owner_name="fixture",
        group_id="other",
        participant_id="peer",
        config={},
    )
    await _admit(
        manager,
        other_group.address,
        "foreign",
        RunExecutionOwner("fixture", "other", "peer", other_group.generation_id, "epoch"),
        input_id="initial:foreign",
    )
    for number in range(10):
        await _admit(manager, binding.address, f"run{number}", owner, f"input{number}")
    with pytest.raises(ChatSessionError):
        await _admit(manager, binding.address, "duplicate-input", owner, "input0")
    complete_run(manager.get(binding.address), _summary("run6"))

    wanted = [f"run{number}" for number in range(0, 10, 3)] + ["foreign", "missing"]
    by_id = manager.owned_runs_by_id_async
    records = await by_id(owner_name="fixture", group_id="group", run_ids=[*wanted, "run0"])
    assert sorted(records) == sorted(wanted[:-2])
    assert records["run0"].address == binding.address
    assert records["run0"].owner == owner
    assert records["run0"].input_id == "input0"
    assert records["run0"].terminal_status is None
    assert records["run9"].record_key > records["run0"].record_key
    paged = {
        record.run_id: record
        for record in manager.owned_runs(owner_name="fixture", group_id="group", limit=1000)
    }
    assert records["run6"] == paged["run6"]
    assert records["run6"].terminal_status == "completed"
    assert not await by_id(owner_name="other", group_id="group", run_ids=["run0"])
    assert await by_id(owner_name="fixture", group_id="group", run_ids=[]) == {}

    by_input = manager.owned_run_by_input_async
    assert await by_input(binding.address, "input6") == paged["run6"]
    assert await by_input(binding.address, "missing") is None
    assert await by_input(binding.address, "initial:foreign") is None
    assert await by_input(SessionAddress(None, "temporary", "gone"), "x") is None
    with pytest.raises(ValueError):
        await by_input(binding.address, "")
    with pytest.raises(ValueError):
        await by_id(owner_name="fixture", group_id="group", run_ids=[""])

    assert await manager.delete_temporary_group(owner_name="fixture", group_id="group") == 1
    assert await by_input(binding.address, "input6") is None
    assert not await by_id(owner_name="fixture", group_id="group", run_ids=["run6"])


@pytest.mark.asyncio
async def test_group_titles_label_owned_summaries_and_leave_with_their_group(manager) -> None:
    binding, _owner = _bind(manager)
    second_binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary-2", "participant-2"),
        owner_name="fixture",
        group_id="group",
        participant_id="peer-2",
        config={"name": "Xenia", "model": "provider/model", "instructions": "private"},
    )
    manager.create_bound_temporary_session(
        SessionAddress(None, "temporary-3", "participant-3"),
        owner_name="other",
        group_id="group",
        participant_id="peer",
        config={},
    )
    ordinary = manager.create("agent")

    def set_title(title: str) -> None:
        manager.set_temporary_group_title(owner_name="fixture", group_id="group", title=title)

    async def titles(owner_name: str, *group_ids: str) -> dict[str, str]:
        stored: dict[str, str] = await manager.temporary_group_titles_async(
            owner_name=owner_name, group_ids=group_ids
        )
        return stored

    set_title("First")
    set_title("Parser")
    for invalid in ("", " padded ", "two\nlines", "x" * 121):
        with pytest.raises(ChatSessionError):
            set_title(invalid)

    assert await titles("fixture", "group", "missing") == {"group": "Parser"}
    assert await titles("other", "group") == {}
    owned = manager.list_owned_session_summaries(owner_name="fixture", group_id="group")
    assert [entry.address for entry in owned] == [binding.address, second_binding.address]
    assert [entry.group_title for entry in owned] == ["Parser", "Parser"]
    assert (owned[1].participant_name, owned[1].model) == ("Xenia", "provider/model")
    assert owned[1].summary["id"] == "participant-2"
    assert "instructions" not in owned[1].summary
    assert {entry.owner_name for entry in manager.list_owned_session_summaries()} == {
        "fixture",
        "other",
    }
    assert manager.list_addresses(exclude_owner_managed=True) == [ordinary.address]

    await manager.delete_temporary_group(owner_name="fixture", group_id="group")

    assert await titles("fixture", "group") == {}
    assert [entry.owner_name for entry in manager.list_owned_session_summaries()] == ["other"]
