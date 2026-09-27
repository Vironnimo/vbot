"""Pinned Memory RPCs: list, add, replace and remove for an Identity Agent's Workspace."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Literal

import pytest

from core.agents import AgentStore
from tests.server.rpc_test_support import (
    StubAdapter,
    call,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)

_CODER_MEMORIES = {"kind": "memories", "scope": {"agent_id": "coder"}}


@pytest.mark.asyncio
async def test_memory_crud_works_independently_of_prompt_mode(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    # Mode controls prompt visibility only; the CRUD surface works when it is off.
    agent = state.runtime.agents.update(
        "coder", memory_prompt_mode="off", workspace=str(tmp_path / "workspace")
    )

    listed = await rpc_result(state, "memory.list", agent_id="coder")
    added_agent = await rpc_result(
        state, "memory.add", agent_id="coder", scope="agent", content="  Keep releases small.  "
    )
    added_user = await rpc_result(
        state, "memory.add", agent_id="coder", scope="user", content="Prefers concise answers."
    )
    replaced = await rpc_result(
        state,
        "memory.replace",
        agent_id="coder",
        scope="agent",
        entry_id=1,
        content="Keep releases focused.",
    )
    removed = await rpc_result(state, "memory.remove", agent_id="coder", scope="user", entry_id=1)

    assert listed == {"agent_id": "coder", "scopes": {"agent": [], "user": []}}
    assert added_agent["entry"] == {"id": 1, "scope": "agent", "content": "Keep releases small."}
    assert added_user["scopes"]["user"][0]["content"] == "Prefers concise answers."
    assert replaced["scopes"] == {
        "agent": [{"id": 1, "scope": "agent", "content": "Keep releases focused."}],
        "user": [{"id": 1, "scope": "user", "content": "Prefers concise answers."}],
    }
    assert removed["scopes"]["user"] == []
    workspace = Path(agent.workspace)
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == "- Keep releases focused.\n"
    assert (workspace / "USER.md").read_text(encoding="utf-8") == ""
    # Each mutation announces a content-free invalidation scoped to its Agent.
    assert resource_changes(state) == [_CODER_MEMORIES] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "code", "named"),
    [
        (
            "memory.add",
            {"agent_id": "coder", "scope": "other", "content": "Fact"},
            "invalid_request",
            "params.scope must be one of",
        ),
        (
            "memory.remove",
            {"agent_id": "coder", "scope": "agent", "entry_id": 0},
            "invalid_request",
            "params.entry_id must be a positive integer",
        ),
        (
            "memory.list",
            {"agent_id": "coder", "scope": "agent"},
            "invalid_request",
            "scope",
        ),
        # The Agent store owns the not-found contract the RPC code is derived from.
        ("memory.list", {"agent_id": "ghost"}, "agent_not_found", ""),
        (
            "memory.add",
            {"agent_id": "ghost", "scope": "agent", "content": "x"},
            "agent_not_found",
            "",
        ),
        (
            "memory.remove",
            {"agent_id": "ghost", "scope": "user", "entry_id": 1},
            "agent_not_found",
            "",
        ),
    ],
)
async def test_memory_refusals_write_and_publish_nothing(
    tmp_path: Path, method: str, params: dict[str, object], code: str, named: str
) -> None:
    state = make_state(tmp_path, StubAdapter())
    agents = AgentStore(tmp_path / "data")
    state.runtime.agents = agents
    try:
        workspace = Path(agents.create("coder").workspace)

        error = await rpc_error(state, method, **params)
    finally:
        agents.close()

    assert error["code"] == code
    assert named in error["message"]
    assert not (workspace / "MEMORY.md").exists()
    assert not (workspace / "USER.md").exists()
    assert resource_changes(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "cancel_write"),
    [("rename", True), ("relocate", False)],
)
async def test_memory_write_runs_off_the_loop_and_finishes_before_the_workspace_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, cancel_write: bool
) -> None:
    state = make_state(tmp_path, StubAdapter())
    agents = AgentStore(tmp_path, sessions=state.runtime.chat_sessions)
    state.runtime.agents = agents
    monkeypatch.setattr(state.runtime.agent_resolver, "_agents", agents)
    original = agents.create("coder")
    entered = asyncio.Event()
    attempted = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    write_threads: list[int] = []
    memory = state.runtime.memory
    add_entry = memory.add_entry

    class ObservedLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            attempted.set()
            return await super().acquire()

    state.agent_delete_lock = ObservedLock()

    def blocked_add_entry(*args: Any, **kwargs: Any) -> Any:
        write_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return add_entry(*args, **kwargs)

    monkeypatch.setattr(memory, "add_entry", blocked_add_entry)
    adding = asyncio.create_task(
        call(state, "memory.add", agent_id="coder", scope="agent", content="Durable fact.")
    )
    tasks: list[asyncio.Task[Any]] = [adding]
    try:
        # The Event Loop keeps serving requests while the file work is blocked.
        await asyncio.wait_for(entered.wait(), 10)
        assert state.agent_delete_lock.locked()
        if cancel_write:
            adding.cancel()
        attempted.clear()
        if operation == "rename":
            change = call(state, "agent.rename", id="coder", new_id="renamed")
        else:
            change = call(
                state,
                "agent.update",
                id="coder",
                workspace=str(tmp_path / "relocated"),
                copy_workspace_identity_files=True,
            )
        changing = asyncio.create_task(change)
        tasks.append(changing)
        await asyncio.wait_for(attempted.wait(), 10)
        assert state.agent_delete_lock.locked()
        assert not changing.done()
        assert not adding.done()
        assert state.event_bus.events == []
        release.set()
        if cancel_write:
            with pytest.raises(asyncio.CancelledError):
                await adding
        else:
            assert (await adding)["ok"] is True
        assert (await changing)["ok"] is True
        destination = agents.get("renamed" if operation == "rename" else "coder")
        assert [
            entry.content for entry in memory.list_entries(Path(destination.workspace), "agent")
        ] == ["Durable fact."]
        if operation == "rename":
            assert not Path(original.workspace).exists()
        assert write_threads and loop_thread not in write_threads
        assert state.event_bus.events[0]["payload"] == _CODER_MEMORIES
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        agents.close()


@pytest.mark.asyncio
async def test_cancelled_memory_write_waiting_for_agent_reference_lock_never_writes(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    workspace = tmp_path / "workspace"
    state.runtime.agents.update("coder", workspace=str(workspace))
    await state.agent_delete_lock.acquire()
    adding = asyncio.create_task(
        call(state, "memory.add", agent_id="coder", scope="agent", content="Never stored.")
    )
    try:
        await asyncio.sleep(0)
        adding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await adding
    finally:
        state.agent_delete_lock.release()
    assert state.runtime.memory.list_entries(workspace, "agent") == []
    assert state.event_bus.events == []
