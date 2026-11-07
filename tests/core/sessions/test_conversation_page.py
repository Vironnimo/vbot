"""Bounded, generation-checked conversation reads over current Session history."""

from __future__ import annotations

import pytest

from core.chat import ChatMessage
from core.chat.messages import MessageSender
from core.runs import Run
from core.sessions.errors import SessionNotFoundError
from core.subagents.links import link_session


@pytest.mark.asyncio
async def test_conversation_page_follows_lineage_and_excludes_replaced_and_private_text(manager):
    source = manager.create("coder", session_id="source")
    first = ChatMessage.user("First task")
    source.append_many(
        [
            first,
            ChatMessage.assistant(model="test", content="First answer", reasoning="PRIVATE"),
            ChatMessage.note("PRIVATE NOTE"),
        ]
    )
    fork = await manager.fork(source.address)
    fork = manager.get(fork.address)
    question = ChatMessage.user("Current task " + "x" * 150)
    peer = ChatMessage.user(
        "Peer question", sender=MessageSender("other-ref", "reviewer", "member", kind="session")
    )
    fork.append_many([question, peer, ChatMessage.assistant(model="test", content="Latest answer")])
    generation = fork.generation_id
    assert generation is not None

    page = manager.conversation_page(
        fork.address, expected_generation_id=generation, limit=2, max_text_chars=40
    )
    assert [message.text for message in page.messages] == ["Peer question", "Latest answer"]
    assert page.latest_user is not None and page.latest_user.message_id == question.id
    assert page.latest_user.truncated and len(page.latest_user.text) == 40
    assert page.latest_input is not None and page.latest_input.message_id == peer.id
    assert page.latest_input.sender_kind == "session"
    assert page.messages[0].sender_kind == "session"
    assert page.messages[0].sender_id == "other-ref"
    assert page.messages[0].sender_role == "member"
    assert page.has_more
    older = manager.conversation_page(
        fork.address,
        expected_generation_id=generation,
        limit=2,
        before_sequence=page.messages[0].sequence,
        max_text_chars=40,
    )
    assert older.messages[0].text == "First answer"
    assert older.messages[1].message_id == question.id
    assert older.latest_input == page.latest_input
    assert all("PRIVATE" not in item.text for item in (*page.messages, *older.messages))
    filtered = manager.conversation_page(
        fork.address,
        expected_generation_id=generation,
        limit=1,
        max_text_chars=40,
        include_peer_messages=False,
    )
    assert filtered.latest_user == page.latest_user
    assert filtered.latest_input == page.latest_input

    fork.apply_edit(question.id, [ChatMessage.user("Replacement task")])
    edited = manager.conversation_page(fork.address, expected_generation_id=generation, limit=10)
    assert [message.text for message in edited.messages] == [
        "First task",
        "First answer",
        "Replacement task",
    ]
    assert edited.cursor_floor_sequence > 0
    assert edited.latest_input is not None and edited.latest_input.text == "Replacement task"
    # Inherited context remains source-faithful after later source changes.
    source.apply_edit(first.id, [ChatMessage.user("Different source task")])
    inherited = manager.conversation_page(fork.address, expected_generation_id=generation, limit=10)
    assert inherited.messages[0].text == "First task"
    link_session(
        manager,
        fork.address,
        subagent_id="delegated-ref",
        parent=source.address,
        run_id="parent-run",
        tool_call_id="parent-call",
        tool_call_index=0,
    )
    run = Run(run_id="provenance-run", agent_id=fork.address.agent_id, session_id=fork.id)
    await manager.start_run(run)
    fork.for_run(run.id).append_many(
        [ChatMessage.user("Delegated task")], subagent_reply_target="parent"
    )
    provenance = manager.conversation_page(fork.address, expected_generation_id=generation)
    assert provenance.messages[-1].subagent_reply_target == "parent"
    assert provenance.messages[-1].run_id == run.id


def test_conversation_page_refuses_recreated_generation(manager):
    session = manager.create("coder", session_id="same")
    generation = session.generation_id
    assert generation is not None
    manager.delete(session.address)
    replacement = manager.create("coder", session_id="same")
    replacement.append(ChatMessage.user("replacement secret"))
    with pytest.raises(SessionNotFoundError):
        manager.conversation_page(replacement.address, expected_generation_id=generation)
