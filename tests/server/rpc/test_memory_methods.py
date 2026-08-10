"""Pinned Memory RPC handlers."""

from __future__ import annotations

from pathlib import Path

import pytest

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
