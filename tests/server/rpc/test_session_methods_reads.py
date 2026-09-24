"""Session point and activity reads over a real Session store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.sessions import FORK_SOURCE_META_KEY, SESSION_FORK_ALWAYS_STRIP_META_KEYS, SessionAddress
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import StubAdapter, make_state


async def _rpc(state: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await dispatch_rpc(state, {"method": method, "params": params})
    assert response["ok"] is True, response
    result: dict[str, Any] = response["result"]
    return result


@pytest.mark.asyncio
async def test_session_get_reads_one_summary_by_exact_address(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    source = sessions.create("coder", session_id="source")
    source.append(ChatMessage.user("hello"))
    sessions.set_title(source.address, "Release planning")
    fork = await sessions.fork(source.address, strip_meta_keys=SESSION_FORK_ALWAYS_STRIP_META_KEYS)
    sessions.record_terminal_run(fork.address, "run-one", "completed", "2026-09-20T10:00:00Z")

    parent = await _rpc(state, "session.get", {"agent_id": "coder", "session_id": "source"})
    child = await _rpc(state, "session.get", {"agent_id": "coder", "session_id": fork.id})

    assert parent["session"]["id"] == "source"
    assert parent["session"]["agent_address"] == "coder"
    assert parent["session"]["title"] == "Release planning"
    assert "agent_id" not in parent["session"]
    assert "compaction_policy_effective" not in parent["session"]
    assert child["session"][FORK_SOURCE_META_KEY]["session_id"] == "source"
    assert child["session"]["unread_run_id"] == "run-one"


@pytest.mark.asyncio
async def test_session_get_reports_an_absent_session_as_none(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    archived = sessions.create("coder", session_id="archived")
    await sessions.archive(archived.address)
    sessions.create("builder", session_id="team", project_id="vbot")

    assert await _rpc(state, "session.get", {"agent_id": "coder", "session_id": "archived"}) == {
        "session": None
    }
    # The project rides in the address: the same Session id under the identity
    # Agent is a different Session.
    assert await _rpc(state, "session.get", {"agent_id": "builder", "session_id": "team"}) == {
        "session": None
    }
    team = await _rpc(state, "session.get", {"agent_id": "builder@vbot", "session_id": "team"})
    assert team["session"]["agent_address"] == "builder@vbot"


@pytest.mark.asyncio
async def test_session_get_rejects_unsupported_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "session.get",
            "params": {"agent_id": "coder", "session_id": "one", "limit": 1},
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_activity_list_returns_only_completed_sessions_per_address(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())
    sessions = state.runtime.chat_sessions
    for session_id in ("idle", "unread", "read"):
        sessions.create("coder", session_id=session_id)
    sessions.create("builder", session_id="team", project_id="vbot")
    unread = SessionAddress(project_id=None, agent_id="coder", session_id="unread")
    read = SessionAddress(project_id=None, agent_id="coder", session_id="read")
    team = SessionAddress(project_id="vbot", agent_id="builder", session_id="team")
    sessions.record_terminal_run(unread, "run-unread", "failed", "2026-09-20T10:00:00Z")
    sessions.record_terminal_run(read, "run-read", "completed", "2026-09-20T10:01:00Z")
    sessions.mark_terminal_run_read(read, "run-read")
    sessions.record_terminal_run(team, "run-team", "completed", "2026-09-20T10:02:00Z")

    result = await _rpc(
        state,
        "session.activity_list",
        {"agent_ids": ["coder", "builder@vbot", "deleted-agent", "coder"]},
    )

    assert [
        (agent["agent_id"], agent["project_id"], [row["id"] for row in agent["sessions"]])
        for agent in result["agents"]
    ] == [
        ("coder", None, ["read", "unread"]),
        ("builder", "vbot", ["team"]),
        ("deleted-agent", None, []),
    ]
    rows = {row["id"]: row for row in result["agents"][0]["sessions"]}
    assert rows["read"]["has_unread_completion"] is False
    assert rows["read"]["latest_completion_run_id"] == "run-read"
    assert rows["unread"]["unread_run_status"] == "failed"
