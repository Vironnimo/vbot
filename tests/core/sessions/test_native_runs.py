"""Run identity, single-row Tool completion, and durable lifecycle boundaries."""

from __future__ import annotations

import sqlite3

import pytest

from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.chat.messages import ToolCall
from core.runs import ChatRunManager
from tests.core.sessions.sessions_test_support import manager as manager


@pytest.mark.asyncio
async def test_completion_commits_entities_before_publishing_terminal_event(manager):
    session = manager.create("coder")
    runs = ChatRunManager()
    runs.bind_persistence(manager)

    async def execute(run):
        writer = session.for_run(run.id)
        assistant = ChatMessage.assistant(
            model="test",
            content=None,
            tool_calls=[ToolCall(id="call", name="read", arguments={"path": "x"})],
        )
        writer.append_many([ChatMessage.user("read"), assistant])
        writer.assistant_message_id = assistant.id
        writer.append(
            ChatMessage.tool(
                tool_call_id="call", name="read", content='{"ok":true,"data":{"text":"result"}}'
            )
        )
        answer = ChatMessage.assistant(model="test", content="done")
        writer.append(answer)
        return answer

    run = await runs.start(session.address, execute)
    await run.wait()
    assert run.events[-1].payload["history_persisted"] is True
    with sqlite3.connect(manager._store.path) as connection:
        assert connection.execute("SELECT DISTINCT run_id FROM messages").fetchall() == [(run.id,)]
        assert connection.execute("SELECT status FROM runs").fetchall() == [("completed",)]
        assert connection.execute("SELECT status,result_content FROM tool_calls").fetchall() == [
            ("completed", '{"ok":true,"data":{"text":"result"}}')
        ]
        assert connection.execute("SELECT role FROM messages ORDER BY seq").fetchall() == [
            ("user",),
            ("assistant",),
            ("assistant",),
        ]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('tool_messages','run_summaries','run_execution_starts')"
            ).fetchall()
            == []
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    snapshot = session.read_chat_history_snapshot(limit=50)
    assert snapshot.runs[0]["complete"] is True
    assert session.load_run_result(run_id=run.id).assistant.content == "done"
    await runs.aclose()


def test_restart_settles_run_and_unfinished_calls_once(manager):
    session = manager.create("coder").start_run("abandoned")
    session.append(
        ChatMessage.assistant(
            model="test", content=None, tool_calls=[ToolCall(id="call", name="write", arguments={})]
        )
    )
    manager.recover_interrupted_runs()
    first = session.load()
    manager.recover_interrupted_runs()
    assert session.load() == first
    assert session.find_run_summary(run_id="abandoned").status == "interrupted"
    with sqlite3.connect(manager._store.path) as connection:
        assert connection.execute("SELECT status,result_content FROM tool_calls").fetchall() == [
            ("interrupted", None)
        ]
        assert connection.execute("SELECT completion_reason FROM runs").fetchone() == (
            "process_restart",
        )


@pytest.mark.asyncio
async def test_failed_terminal_transaction_never_claims_persistence(manager, monkeypatch):
    session = manager.create("coder")
    runs = ChatRunManager()
    runs.bind_persistence(manager)

    async def fail_finish(*args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(manager, "finish_run", fail_finish)

    async def execute(run):
        session.for_run(run.id).append(ChatMessage.assistant(model="test", content="done"))

    run = await runs.start(session.address, execute)
    with pytest.raises(OSError):
        await run.wait()
    assert run.events[-1].payload["history_persisted"] is False
    assert session.find_run_summary(run_id=run.id) is None
    await runs.aclose()


def test_reused_provider_call_id_requires_exact_assistant_identity(manager):
    session = manager.create("coder").start_run("run")
    for text in ("first", "second"):
        assistant = ChatMessage.assistant(
            model="test",
            content=None,
            tool_calls=[ToolCall(id="reused", name="read", arguments={})],
        )
        session.append(assistant)
        session.assistant_message_id = assistant.id
        session.append(ChatMessage.tool(tool_call_id="reused", name="read", content=text))
    with sqlite3.connect(manager._store.path) as connection:
        assert connection.execute(
            "SELECT result_content FROM tool_calls ORDER BY tool_call_key"
        ).fetchall() == [("first",), ("second",)]


@pytest.mark.asyncio
async def test_fork_is_a_snapshot_not_another_executing_run(manager):
    source = manager.create("coder").start_run("in-flight")
    source.append(ChatMessage.assistant(model="test", content="partial"))
    fork = await manager.fork(source.address)
    manager.recover_interrupted_runs()
    assert [message.content for message in fork.load()] == ["partial"]
    with sqlite3.connect(manager._store.path) as connection:
        inherited = connection.execute(
            "SELECT status,completion_reason,terminal_sequence FROM runs "
            "WHERE origin_generation_id IS NOT NULL"
        ).fetchone()
        assert inherited == ("interrupted", "fork_snapshot", None)
    with pytest.raises(ChatSessionError, match="inherited Run"):
        fork.start_run("in-flight")


@pytest.mark.asyncio
async def test_settled_run_rejects_late_output_and_duplicate_completion(manager):
    session = manager.create("coder")
    runs = ChatRunManager()
    runs.bind_persistence(manager)
    observed = []

    async def execute(run):
        async def completed(status):
            observed.append((str(status), session.find_run_summary(run_id=run.id).status))

        run.add_completion_observer(completed)
        session.for_run(run.id).append(ChatMessage.assistant(model="test", content="done"))

    run = await runs.start(session.address, execute)
    await run.wait()
    assert observed == [("completed", "completed")]
    with pytest.raises(ChatSessionError, match="running Run"):
        session.for_run(run.id).append(ChatMessage.assistant(model="test", content="late"))
    with pytest.raises(ChatSessionError, match="running Run"):
        await manager.finish_run(run, "completed", run.events[-1].payload)
    await runs.aclose()
