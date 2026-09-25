"""Pinned Memory RPC handlers."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from core.agents import AgentStore
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import StubAdapter, make_state


@pytest.mark.asyncio
async def test_memory_list_returns_both_scopes_when_memory_is_off(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update(
        "coder",
        memory_prompt_mode="off",
        workspace=str(tmp_path / "workspace"),
    )

    response = await dispatch_rpc(
        state,
        {"method": "memory.list", "params": {"agent_id": "coder"}},
    )

    assert response == {
        "ok": True,
        "result": {
            "agent_id": "coder",
            "scopes": {"agent": [], "user": []},
        },
    }


@pytest.mark.asyncio
async def test_memory_crud_works_independently_of_prompt_mode(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    agent = state.runtime.agents.update(
        "coder",
        memory_prompt_mode="off",
        workspace=str(tmp_path / "workspace"),
    )

    added_agent = await dispatch_rpc(
        state,
        {
            "method": "memory.add",
            "params": {
                "agent_id": "coder",
                "scope": "agent",
                "content": "  Keep releases small.  ",
            },
        },
    )
    added_user = await dispatch_rpc(
        state,
        {
            "method": "memory.add",
            "params": {
                "agent_id": "coder",
                "scope": "user",
                "content": "Prefers concise answers.",
            },
        },
    )
    replaced = await dispatch_rpc(
        state,
        {
            "method": "memory.replace",
            "params": {
                "agent_id": "coder",
                "scope": "agent",
                "entry_id": 1,
                "content": "Keep releases focused.",
            },
        },
    )
    removed = await dispatch_rpc(
        state,
        {
            "method": "memory.remove",
            "params": {
                "agent_id": "coder",
                "scope": "user",
                "entry_id": 1,
            },
        },
    )

    assert added_agent["result"]["entry"] == {
        "id": 1,
        "scope": "agent",
        "content": "Keep releases small.",
    }
    assert added_user["result"]["scopes"]["user"][0]["content"] == ("Prefers concise answers.")
    assert replaced["result"]["scopes"] == {
        "agent": [
            {
                "id": 1,
                "scope": "agent",
                "content": "Keep releases focused.",
            }
        ],
        "user": [
            {
                "id": 1,
                "scope": "user",
                "content": "Prefers concise answers.",
            }
        ],
    }
    assert removed["result"]["scopes"]["user"] == []
    workspace = Path(agent.workspace)
    assert (workspace / "MEMORY.md").read_text(encoding="utf-8") == ("- Keep releases focused.\n")
    assert (workspace / "USER.md").read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "message"),
    [
        (
            "memory.add",
            {"agent_id": "coder", "scope": "other", "content": "Fact"},
            "params.scope must be one of",
        ),
        (
            "memory.remove",
            {"agent_id": "coder", "scope": "agent", "entry_id": 0},
            "params.entry_id must be a positive integer",
        ),
    ],
)
async def test_memory_mutations_validate_scope_and_entry_id(
    tmp_path: Path,
    method: str,
    params: dict[str, object],
    message: str,
) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert message in response["error"]["message"]


@pytest.mark.asyncio
async def test_memory_mutation_publishes_scoped_invalidation(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", workspace=str(tmp_path / "workspace"))

    response = await dispatch_rpc(
        state,
        {
            "method": "memory.add",
            "params": {
                "agent_id": "coder",
                "scope": "agent",
                "content": "Keep tests deterministic.",
            },
        },
    )

    assert response["ok"] is True
    assert state.event_bus.events[-1]["type"] == "resource_changed"
    assert state.event_bus.events[-1]["payload"] == {
        "kind": "memories",
        "scope": {"agent_id": "coder"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,params",
    [
        ("memory.list", {"agent_id": "ghost"}),
        ("memory.add", {"agent_id": "ghost", "scope": "agent", "content": "x"}),
        ("memory.remove", {"agent_id": "ghost", "scope": "user", "entry_id": 1}),
    ],
)
async def test_memory_unknown_agent_is_agent_not_found(
    tmp_path: Path,
    method: str,
    params: dict[str, object],
) -> None:
    state = make_state(tmp_path, StubAdapter())
    # The real store owns the not-found contract the RPC code is derived from.
    state.runtime.agents = AgentStore(tmp_path / "data")

    response = await dispatch_rpc(state, {"method": method, "params": params})

    assert response["ok"] is False
    assert response["error"]["code"] == "agent_not_found"
    assert state.event_bus.events == []


@pytest.mark.asyncio
async def test_memory_rpc_keeps_the_event_loop_responsive_during_file_work(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path, StubAdapter())
    state.runtime.agents.update("coder", workspace=str(tmp_path / "workspace"))
    memory = state.runtime.memory
    entered = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    threads: list[int] = []
    add_entry = memory.add_entry

    def blocked_add_entry(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5)
        return add_entry(*args, **kwargs)

    memory.add_entry = blocked_add_entry
    adding = asyncio.create_task(
        dispatch_rpc(
            state,
            {
                "method": "memory.add",
                "params": {"agent_id": "coder", "scope": "agent", "content": "Stay async."},
            },
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        loop = asyncio.get_running_loop()
        ticked_at = loop.time()
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert loop.time() - ticked_at < 1
        assert not adding.done()
        assert state.event_bus.events == []
    finally:
        release.set()

    response = await asyncio.wait_for(adding, timeout=5)
    assert response["ok"] is True
    assert response["result"]["scopes"]["agent"] == [
        {"id": 1, "scope": "agent", "content": "Stay async."}
    ]
    assert threads and loop_thread not in threads
    assert state.event_bus.events[-1]["payload"] == {
        "kind": "memories",
        "scope": {"agent_id": "coder"},
    }
