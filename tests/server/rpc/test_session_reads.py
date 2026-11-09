"""Operator Session reads resolve ids, preserve scope and return actionable pages."""

from pathlib import Path

import pytest

from core.chat import ChatMessage
from core.subagents.links import link_session
from tests.server.rpc_test_support import StubAdapter, make_state, rpc_error, rpc_result


@pytest.mark.asyncio
async def test_session_reads_resolve_ids_and_page_across_agents(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    root = sessions.create("main", session_id="shared")
    root.append_many(
        [ChatMessage.user("Root task"), ChatMessage.assistant(model="test", content="Progress")]
    )
    child = sessions.create("helper", session_id="child", metadata={"title": "Review changes"})
    link_session(
        sessions,
        child.address,
        subagent_id="sub-one",
        parent=root.address,
        run_id="parent-run",
        tool_call_id="parent-call",
        tool_call_index=0,
    )
    listing = await rpc_result(state, "session.list", limit=1)
    assert listing["total_count"] == 2
    assert len(listing["sessions"]) == 1
    older = await rpc_result(state, "session.list", limit=1, cursor=listing["next_cursor"])
    assert older["sessions"][0]["id"] != listing["sessions"][0]["id"]
    page = await rpc_result(state, "session.show", session_id=root.id, summary=True, limit=1)
    assert page["task"]["text"] == "Root task"
    assert page["latest_answer"]["text"] == "Progress"
    assert page["subagents"][0]["id"] == child.id
    assert page["subagent_count"] == 1
    previous = await rpc_result(
        state, "session.show", session_id=root.id, summary=True, cursor=page["next_cursor"]
    )
    assert previous["entries"][0]["text"] == "Root task"
    children = await rpc_result(state, "session.list", parent_session_id=root.id)
    assert [row["id"] for row in children["sessions"]] == [child.id]

    sessions.create("other", session_id=root.id)
    error = await rpc_error(state, "session.show", session_id=root.id, summary=True)
    assert error["code"] == "session_id_ambiguous"
    assert error["data"]["agents"] == ["main", "other"]
    parent_error = await rpc_error(state, "session.list", parent_session_id=root.id)
    assert "--parent-agent" in parent_error["message"]
    children = await rpc_result(
        state, "session.list", parent_session_id=root.id, parent_agent_id="main"
    )
    assert [row["id"] for row in children["sessions"]] == [child.id]
    exact = await rpc_result(
        state, "session.show", session_id=root.id, agent_id="main", summary=True
    )
    assert exact["session"]["agent_address"] == "main"
    missing = await rpc_error(state, "session.show", session_id="missing")
    assert missing["code"] == "session_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"summary": True, "entry": 0},
        {"offset": 1},
        {"limit": 101},
        {"entry": -1},
        {"cursor": "invalid"},
        {"run_id": "missing"},
    ],
)
async def test_session_inspection_rejects_invalid_selections(tmp_path: Path, params) -> None:
    state = make_state(tmp_path, StubAdapter())
    session = state.runtime.chat_sessions.create("main")
    error = await rpc_error(state, "session.show", session_id=session.id, **params)
    assert error["code"] in {"invalid_request", "domain_error"}
