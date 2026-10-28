"""Ordinary Accessor acceptance, queue recovery and atomic consumption contracts."""

from __future__ import annotations

import pytest

from core.chat import ChatMessage
from core.chat.content_blocks import FileBlock
from core.chat.errors import ChatSessionError
from core.runs import Run
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.errors import AccessorInputConflictError


async def _accept(manager, address, input_id, *, payload=None):
    return await manager.accept_accessor_input_async(
        address,
        input_id,
        request_hash=f"original-{input_id}",
        payload=payload or {"format_version": 1, "content": input_id},
        queue_id=f"queue-{input_id}",
        expected_generation_id=manager.get(address).generation_id,
    )


def _run(address: SessionAddress, run_id: str, input_id: str) -> Run:
    run = Run(
        run_id=run_id,
        agent_id=address.agent_id,
        session_id=address.session_id,
        project_id=address.project_id,
    )
    run.accessor_input_id = input_id
    return run


@pytest.mark.asyncio
async def test_acceptance_edits_and_removed_receipts_survive_restart(manager):
    session = manager.create("coder")
    original, created = await _accept(
        manager, session.address, "first", payload={"content": "before", "future": {"keep": 1}}
    )
    assert created
    updated = await manager.update_accessor_input_async(
        session.address, "first", payload_patch={"content": "after"}
    )
    assert updated.payload == {"content": "after", "future": {"keep": 1}}
    assert updated.request_hash == original.request_hash
    await _accept(manager, session.address, "removed")
    assert await manager.remove_accessor_input_async(session.address, "removed")
    assert not await manager.remove_accessor_input_async(session.address, "removed")
    await _accept(manager, session.address, "last")
    manager.close()
    reopened = ChatSessionManager(manager.data_dir)
    try:
        assert [record.input_id for record in await reopened.queued_accessor_inputs_async()] == [
            "first",
            "last",
        ]
        # Lookup is global, before any attempted new-Session allocation or live-address read.
        retry, created = await reopened.accept_accessor_input_async(
            SessionAddress(None, "uncreated", "missing"),
            "first",
            request_hash=original.request_hash,
            payload={"content": "before"},
            queue_id="replacement-queue-id",
        )
        assert not created
        assert retry == updated
        with pytest.raises(AccessorInputConflictError):
            await reopened.lookup_accessor_input_async("first", request_hash="different-content")
        removed, created = await _accept(reopened, session.address, "removed")
        assert not created and removed.state == "removed"
        assert [record.queue_id for record in await reopened.queued_accessor_inputs_async()] == [
            "queue-first",
            "queue-last",
        ]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_run_admission_consumes_input_once_and_restart_never_requeues_it(manager):
    session = manager.create("coder")
    await _accept(manager, session.address, "started")
    run = _run(session.address, "admitted", "started")
    await manager.start_run(run)
    await manager.start_run(run)
    record = await manager.lookup_accessor_input_async("started")
    assert (record.state, record.run_id, record.run_status) == ("started", "admitted", "running")
    with pytest.raises(ChatSessionError):
        await manager.start_run(_run(session.address, "duplicate", "started"))
    await _accept(manager, session.address, "second")
    with pytest.raises(ChatSessionError):
        await manager.start_run(_run(session.address, "admitted", "second"))
    assert await manager.remove_accessor_input_async(session.address, "second")
    assert [stored.run_id for stored in session.run_records()] == ["admitted"]
    manager.recover_interrupted_runs()
    assert await manager.queued_accessor_inputs_async() == ()
    record = await manager.lookup_accessor_input_async("started")
    assert (record.state, record.run_id, record.run_status) == (
        "started",
        "admitted",
        "interrupted",
    )
    await _accept(manager, session.address, "removed")
    assert await manager.remove_accessor_input_async(session.address, "removed")
    with pytest.raises(ChatSessionError):
        await manager.start_run(_run(session.address, "must-not-exist", "removed"))
    assert [stored.run_id for stored in session.run_records()] == ["admitted"]


@pytest.mark.asyncio
async def test_steering_commits_receipts_with_carriers_and_replays_no_carriers(manager):
    session = manager.create("coder")
    writer = session.start_run("active")
    await _accept(manager, session.address, "steer")
    user = ChatMessage.user("steered input")
    # The first carrier is inserted before the invalid Tool result rejects the append.
    with pytest.raises(ChatSessionError):
        await writer.append_many_async(
            [user, ChatMessage.tool(tool_call_id="missing", name="missing", content="result")],
            accessor_input_ids=("steer",),
        )
    assert session.load() == []
    assert (await manager.lookup_accessor_input_async("steer")).state == "queued"
    await writer.append_many_async([user], accessor_input_ids=("steer",))
    await writer.append_many_async([user], accessor_input_ids=("steer",))
    assert [message.content for message in session.load()] == ["steered input"]
    record = await manager.lookup_accessor_input_async("steer")
    assert (record.state, record.run_id) == ("steered", "active")
    await _accept(manager, session.address, "other")
    with pytest.raises(ChatSessionError):
        await writer.append_many_async(
            [ChatMessage.user("mixed replay")], accessor_input_ids=("steer", "other")
        )
    assert (await manager.lookup_accessor_input_async("other")).state == "queued"
    manager.recover_interrupted_runs()
    assert [record.input_id for record in await manager.queued_accessor_inputs_async()] == ["other"]


@pytest.mark.asyncio
async def test_receipts_follow_generation_moves_and_never_transfer_to_forks_or_recreated_addresses(
    manager,
):
    source = manager.create("coder")
    original, _ = await _accept(manager, source.address, "input")
    fork = await manager.fork(source.address)
    with pytest.raises(ChatSessionError):
        await manager.start_run(_run(fork.address, "foreign-run", "input"))
    target = SessionAddress(None, "reviewer", "moved")
    await manager.move(source.address, target)
    moved = await manager.lookup_accessor_input_async("input")
    assert moved.address == target and moved.generation_id == original.generation_id
    await manager.archive(target)
    replacement = manager.create("reviewer", session_id=target.session_id)
    archived = await manager.lookup_accessor_input_async("input")
    assert not archived.live
    assert archived.generation_id != manager.get(replacement.address).generation_id
    assert await manager.queued_accessor_inputs_async() == ()
    with pytest.raises(ChatSessionError):
        await manager.start_run(_run(replacement.address, "new-generation", "input"))


@pytest.mark.asyncio
@pytest.mark.parametrize("written", ["none", "notes", "user"])
async def test_restart_preserves_accepted_carrier_before_its_first_user_append(manager, written):
    session = manager.create("coder")
    original = ChatMessage.user("before edit", input_origin="speech_transcription")
    await manager.accept_accessor_input_async(
        session.address,
        "input",
        request_hash="original-input",
        payload={"opaque_execution_format": 42},
        queue_id="queue-input",
        fallback_input=original,
        fallback_notes=(ChatMessage.note("old form values"),),
    )
    carrier = ChatMessage.user(
        [FileBlock("file", "att_accepted", "accepted.txt", "text/plain")],
        input_origin="speech_transcription",
    )
    note = ChatMessage.note("accepted form values")
    await manager.update_accessor_input_async(
        session.address,
        "input",
        payload_patch={"opaque_execution_format": 43},
        fallback_input=carrier,
        fallback_notes=(note,),
        attachment_ids=("att_accepted",),
    )
    assert (await manager.lookup_accessor_input_async("input")).payload == {
        "opaque_execution_format": 43
    }
    await manager.start_run(_run(session.address, "interrupted", "input"))
    if written == "notes":
        await session.for_run("interrupted").append_many_async([note])
    elif written == "user":
        # Normal preparation can choose a new canonical Message identity.
        carrier = ChatMessage.user(carrier.content, input_origin=carrier.input_origin)
        await session.for_run("interrupted").append_many_async([note, carrier])
    manager.recover_interrupted_runs()
    first = session.load()
    manager.recover_interrupted_runs()
    assert session.load() == first
    assert [message.role for message in first] == ["note", "user", "run_summary"]
    assert first[0].to_dict() == {**note.to_dict(), "run_id": "interrupted"}
    assert first[1].to_dict() == {**carrier.to_dict(), "run_id": "interrupted"}
    assert first[2].completion_reason == "process_restart"
    assert manager.referenced_attachment_ids() == {"att_accepted"}
    assert (await manager.lookup_accessor_input_async("input")).attachment_ids == ()
    assert (await manager.lookup_accessor_input_async("input")).payload == {}
    assert await manager.queued_accessor_inputs_async() == ()
