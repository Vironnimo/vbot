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
    child.start_run("old").append(summary("old"))
    await sessions.record_run_owner_async(child.address, run_id="owned", owner=owner)
    child.append(ChatMessage.user("owned work"))
    child.start_run("foreign").append(summary("foreign"))
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
async def test_owned_run_point_lookups_resolve_exact_ids_and_inputs(tmp_path):
    sessions, binding, owner = make_sessions(tmp_path)
    try:
        other_group = sessions.create_bound_temporary_session(
            SessionAddress(None, "temporary", "other-participant"),
            owner_name="fixture",
            group_id="other",
            participant_id="peer",
            config={},
        )
        await sessions.record_run_owner_async(
            other_group.address,
            run_id="foreign",
            owner=RunExecutionOwner("fixture", "other", "peer", other_group.generation_id, "epoch"),
            input_id="initial:foreign",
        )
        for number in range(130):
            await sessions.record_run_owner_async(
                binding.address,
                run_id=f"run{number}",
                owner=owner,
                input_id=f"input{number}",
            )
        sessions.get(binding.address).append(summary("run6"))

        wanted = [f"run{number}" for number in range(0, 130, 3)] + ["foreign", "missing"]
        records = await sessions.owned_runs_by_id_async(
            owner_name="fixture", group_id="group", run_ids=[*wanted, "run0"]
        )
        # Ids beyond one bounded statement are read in chunks of the same snapshot.
        assert sorted(records) == sorted(wanted[:-2])
        assert records["run0"].address == binding.address
        assert records["run0"].owner == owner
        assert records["run0"].input_id == "input0"
        assert records["run0"].terminal_status is None
        assert records["run129"].record_key > records["run0"].record_key
        paged = {
            record.run_id: record
            for record in sessions.owned_runs(owner_name="fixture", group_id="group", limit=1000)
        }
        assert records["run6"] == paged["run6"]
        assert records["run6"].terminal_status == "completed"
        by_id = sessions.owned_runs_by_id_async
        assert not await by_id(owner_name="other", group_id="group", run_ids=["run0"])
        assert await by_id(owner_name="fixture", group_id="group", run_ids=[]) == {}

        by_input = await sessions.owned_run_by_input_async(binding.address, "input6")
        assert by_input == paged["run6"]
        by_input_of = sessions.owned_run_by_input_async
        assert await by_input_of(binding.address, "missing") is None
        assert await by_input_of(binding.address, "initial:foreign") is None
        assert await by_input_of(SessionAddress(None, "temporary", "gone"), "x") is None
        with pytest.raises(ValueError):
            await by_input_of(binding.address, "")
        with pytest.raises(ValueError):
            await by_id(owner_name="fixture", group_id="group", run_ids=[""])

        assert await sessions.delete_temporary_group(owner_name="fixture", group_id="group") == 1
        assert await by_input_of(binding.address, "input6") is None
        assert not await by_id(owner_name="fixture", group_id="group", run_ids=["run6"])
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


@pytest.mark.asyncio
async def test_group_titles_label_owned_summaries_and_leave_with_their_group(tmp_path):
    sessions, binding, _owner = make_sessions(tmp_path)
    second_binding = sessions.create_bound_temporary_session(
        SessionAddress(None, "temporary-2", "participant-2"),
        owner_name="fixture",
        group_id="group",
        participant_id="peer-2",
        config={"name": "Xenia", "model": "provider/model", "instructions": "private"},
    )
    sessions.create_bound_temporary_session(
        SessionAddress(None, "temporary-3", "participant-3"),
        owner_name="other",
        group_id="group",
        participant_id="peer",
        config={},
    )
    ordinary = sessions.create("agent")

    def set_title(title: str) -> None:
        sessions.set_temporary_group_title(owner_name="fixture", group_id="group", title=title)

    async def titles(owner_name: str, *group_ids: str) -> dict[str, str]:
        stored: dict[str, str] = await sessions.temporary_group_titles_async(
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
    owned = sessions.list_owned_session_summaries(owner_name="fixture", group_id="group")
    assert [entry.address for entry in owned] == [binding.address, second_binding.address]
    assert [entry.group_title for entry in owned] == ["Parser", "Parser"]
    assert (owned[1].participant_name, owned[1].model) == ("Xenia", "provider/model")
    assert owned[1].summary["id"] == "participant-2"
    assert "instructions" not in owned[1].summary
    assert {entry.owner_name for entry in sessions.list_owned_session_summaries()} == {
        "fixture",
        "other",
    }
    assert sessions.list_addresses(exclude_owner_managed=True) == [ordinary.address]

    await sessions.delete_temporary_group(owner_name="fixture", group_id="group")

    assert await titles("fixture", "group") == {}
    assert [entry.owner_name for entry in sessions.list_owned_session_summaries()] == ["other"]
