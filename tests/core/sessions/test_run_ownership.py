from dataclasses import replace

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.runs import RunExecutionOwner
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions.format import write_bootstrap_marker


def make_sessions(tmp_path):
    write_bootstrap_marker(tmp_path)
    sessions = ChatSessionManager(tmp_path)
    binding = sessions.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="fixture",
        group_id="group",
        participant_id="peer",
        config={},
    )
    owner = RunExecutionOwner("fixture", "group", "peer", binding.generation_id, "epoch")
    return sessions, binding, owner


def summary(run_id, status="completed"):
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


@pytest.mark.asyncio
async def test_owned_run_survives_reopen_and_requires_exact_durable_terminal(tmp_path):
    sessions, binding, owner = make_sessions(tmp_path)
    child = sessions.create("existing", "target")
    child.append(ChatMessage.user("older unrelated work"))
    child.append(summary("old"))
    await sessions.record_run_owner_async(child.address, run_id="owned", owner=owner)
    child.append(ChatMessage.user("owned work"))
    child.append(summary("foreign"))
    rows = await sessions.owned_runs_async(owner_name="fixture", group_id="group")
    assert len(rows) == 1
    assert rows[0].owner == owner
    assert rows[0].address == child.address
    assert rows[0].start_sequence == 2
    assert rows[0].terminal_status is None
    assert sessions.temporary_binding(child.address) is None
    await sessions.record_run_owner_async(child.address, run_id="owned", owner=owner)
    child.append(summary("owned"))
    sessions.close()

    reopened = ChatSessionManager(tmp_path)
    try:
        records = await reopened.owned_runs_async(owner_name="fixture", group_id="group")
        assert records[0].start_sequence == 2
        assert records[0].terminal_status == "completed"
        assert records[0].terminal_sequence == 4
        assert records[0].owner.generation_id == binding.generation_id
        assert records[0].generation_id != binding.generation_id
        assert not await reopened.owned_runs_async(owner_name="other", group_id="group")
        assert not await reopened.owned_runs_async(owner_name="fixture", group_id="other")
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_owner_claim_cannot_rebind_and_forked_history_has_no_execution_owner(tmp_path):
    sessions, binding, owner = make_sessions(tmp_path)
    try:
        await sessions.record_run_owner_async(binding.address, run_id="run", owner=owner)
        with pytest.raises(ChatSessionError):
            await sessions.record_run_owner_async(
                binding.address,
                run_id="run",
                owner=replace(owner, epoch="other"),
            )
        with pytest.raises(ChatSessionError):
            await sessions.record_run_owner_async(
                binding.address,
                run_id="forged",
                owner=replace(owner, generation_id="old"),
            )
        session = sessions.get(binding.address)
        session.append(ChatMessage.user("goal"))
        session.append(summary("run", "cancelled"))
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            await sessions.fork(binding.address)
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            await sessions.fork(binding.address, target_agent_id=binding.address.agent_id)
        fork = await sessions.fork(binding.address, target_agent_id="ordinary")
        assert len(fork.load()) == 2
        assert sessions.temporary_binding(fork.address) is None
        records = sessions.owned_runs(owner_name="fixture", group_id="group")
        assert len(records) == 1
        assert records[0].address == binding.address
        assert records[0].terminal_status == "cancelled"
        with pytest.raises(ChatSessionError, match="managed by an Extension"):
            await sessions.archive(binding.address)
        assert sessions.owned_runs(owner_name="fixture", group_id="group") == records
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_owner_pages_are_bounded_and_partition_participants(tmp_path):
    sessions, binding, owner = make_sessions(tmp_path)
    try:
        for number in range(3):
            await sessions.record_run_owner_async(
                binding.address,
                run_id=f"run{number}",
                owner=owner,
            )
        first = sessions.owned_runs(owner_name="fixture", group_id="group", limit=2)
        second = sessions.owned_runs(
            owner_name="fixture",
            group_id="group",
            after=first[-1].record_key,
            limit=2,
        )
        assert [row.run_id for row in first + second] == ["run0", "run1", "run2"]
        assert not sessions.owned_runs(
            owner_name="fixture",
            group_id="group",
            participant_id="other",
        )
        for invalid in (True, 0, 1001, "20"):
            with pytest.raises(ValueError):
                sessions.owned_runs(owner_name="fixture", group_id="group", limit=invalid)
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_run_start_boundaries_include_owned_and_ordinary_successors(tmp_path):
    sessions, binding, owner = make_sessions(tmp_path)
    try:
        session = sessions.create("reused", "target")
        await sessions.record_run_owner_async(session.address, run_id="owned", owner=owner)
        session.append(ChatMessage.assistant(model="model", content="owned"))
        await sessions.record_run_start_async(session.address, run_id="ordinary")
        session.append(ChatMessage.assistant(model="model", content="ordinary"))
        session.append(summary("ordinary"))

        boundaries = sessions.run_start_boundaries([session.address])

        assert [(row.run_id, row.start_sequence) for row in boundaries] == [
            ("owned", 0),
            ("ordinary", 1),
        ]
    finally:
        sessions.close()
