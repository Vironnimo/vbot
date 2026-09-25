"""Tool result payloads: stored with their Tool Result, visible through the current view."""

from __future__ import annotations

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.chat.messages import ToolCall
from core.sessions import (
    ChatSession,
    ChatSessionManager,
    SessionAddress,
    ToolResultFacts,
    ToolResultPayload,
)
from tests.core.sessions.history_fixtures import complete_run


def _summary(run_id: str) -> ChatMessage:
    return ChatMessage.run_summary(
        run_id=run_id,
        status="completed",
        iteration_count=1,
        timing={
            "started_at": "2026-09-19T10:00:00Z",
            "completed_at": "2026-09-19T10:00:01Z",
            "duration_ms": 1000,
        },
    )


def _payload_run(session: ChatSession, run_id: str, *payloads: ToolResultPayload) -> ChatMessage:
    """Write one Run whose Tool call returned *payloads*; return its question."""
    run = session.start_run(run_id)
    question = ChatMessage.user(f"{run_id} question")
    assistant = ChatMessage.assistant(
        model="model",
        content=None,
        tool_calls=[ToolCall(id=f"call-{run_id}", name="mcp_example", arguments={})],
    )
    run.append_many([question, assistant])
    run.assistant_message_id = assistant.id
    run.append_many(
        [ChatMessage.tool(tool_call_id=f"call-{run_id}", name="mcp_example", content="receipt")],
        tool_results={f"call-{run_id}": ToolResultFacts("completed", True, payloads=payloads)},
    )
    complete_run(run, _summary(run_id))
    return question


def _payload(payload_id: str, owner: str = "mcp", value: str = '{"rows":[1]}') -> ToolResultPayload:
    return ToolResultPayload(payload_id=payload_id, owner_name=owner, payload_json=value)


async def _load(
    manager: ChatSessionManager, address: SessionAddress, payload_id: str, owner: str = "mcp"
) -> object:
    return await manager.tool_result_payload_async(address, payload_id, owner_name=owner)


@pytest.mark.asyncio
async def test_payload_is_visible_to_its_owner_in_its_session_only(tmp_path) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="source")
    other = manager.create("agent", session_id="other")
    _payload_run(session, "run-one", _payload("res_one"), _payload("res_two", value='"text"'))
    _payload_run(other, "run-other", _payload("res_foreign"))

    assert await _load(manager, session.address, "res_one") == {"rows": [1]}
    assert await _load(manager, session.address, "res_two") == "text"
    # Another Extension, another Session, an unknown id or Session: nothing.
    assert await _load(manager, session.address, "res_one", owner="swarm") is None
    assert await _load(manager, other.address, "res_one") is None
    assert await _load(manager, session.address, "res_foreign") is None
    assert await _load(manager, session.address, "res_missing") is None
    assert await _load(manager, SessionAddress(None, "agent", "gone"), "res_one") is None


@pytest.mark.asyncio
async def test_forks_read_inherited_payloads_even_after_the_ancestor_is_deleted(
    tmp_path,
) -> None:
    manager = ChatSessionManager(tmp_path)
    source = manager.create("agent", session_id="source")
    question = _payload_run(source, "run-one", _payload("res_one"))
    child = await manager.fork(source.address)
    grandchild = await manager.fork(child.address)
    for fork in (child, grandchild):
        assert await _load(manager, fork.address, "res_one") == {"rows": [1]}

    # An edit that removes the Tool Result from the source's view hides its
    # payload there; the forks' frozen views still show it.
    source.apply_edit(question.id, [ChatMessage.user("rewritten")])
    assert await _load(manager, source.address, "res_one") is None
    assert await _load(manager, child.address, "res_one") == {"rows": [1]}

    manager.delete(source.address)

    for fork in (child, grandchild):
        assert await _load(manager, fork.address, "res_one") == {"rows": [1]}
        assert await _load(manager, fork.address, "res_one", owner="swarm") is None


@pytest.mark.parametrize(
    "payloads",
    [
        (_payload("res_one"), _payload("res_one")),
        (_payload("../outside"),),
        (_payload("res_one", owner=""),),
    ],
)
def test_invalid_payloads_are_refused_with_their_tool_result(tmp_path, payloads) -> None:
    manager = ChatSessionManager(tmp_path)
    session = manager.create("agent", session_id="source")

    with pytest.raises(ChatSessionError, match="Tool result facts are invalid"):
        _payload_run(session, "run-one", *payloads)

    assert [message.role for message in session.load()] == ["user", "assistant"]
