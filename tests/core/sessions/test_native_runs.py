"""Run identity, single-row Tool completion, and durable lifecycle boundaries."""

from __future__ import annotations

import sqlite3

import pytest

from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.chat.messages import ToolCall
from core.runs import ChatRunManager, RunAdmission, RunKind
from core.sessions import SESSION_RUN_KINDS_META_KEY
from tests.core.sessions.history_fixtures import complete_run
from tests.core.sessions.sessions_test_support import manager as manager


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


@pytest.mark.asyncio
async def test_completion_commits_entities_before_publishing_terminal_event(manager):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)

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
        assert connection.execute(
            "SELECT DISTINCT r.run_id FROM entries AS e JOIN runs AS r ON r.run_key = e.run_key"
        ).fetchall() == [(run.id,)]
        assert connection.execute("SELECT status FROM runs").fetchall() == [("completed",)]
        # One Tool call row carries the result's outcome and points at its result entry.
        assert connection.execute(
            "SELECT c.status, t.content FROM tool_calls AS c "
            "JOIN entry_text AS t ON t.entry_key = c.result_entry_key"
        ).fetchall() == [("completed", '{"ok":true,"data":{"text":"result"}}')]
        assert connection.execute("SELECT role FROM entries ORDER BY seq").fetchall() == [
            ("user",),
            ("assistant",),
            ("tool",),
            ("assistant",),
            ("run_summary",),
        ]
        assert connection.execute(
            "SELECT e.role FROM runs AS r JOIN entries AS e ON e.entry_key = r.end_entry_key"
        ).fetchall() == [("run_summary",)]
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


@pytest.mark.asyncio
async def test_run_admission_records_its_run_kind_in_the_same_transaction(manager, monkeypatch):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)
    writes = 0
    execute_write = manager._store._execute_write

    def counting_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return execute_write(*args, **kwargs)

    monkeypatch.setattr(manager._store, "_execute_write", counting_write)

    async def execute(run):
        assert writes == 1
        assert manager.get_metadata(session.address)[SESSION_RUN_KINDS_META_KEY] == ["cron"]
        return None

    run = await runs.start(session.address, execute, admission=RunAdmission(run_kind=RunKind.CRON))
    await run.wait()

    assert manager.get_metadata(session.address)[SESSION_RUN_KINDS_META_KEY] == ["cron"]


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
        assert connection.execute("SELECT status, result_entry_key FROM tool_calls").fetchall() == [
            ("interrupted", None)
        ]
        assert connection.execute("SELECT completion_reason FROM runs").fetchone() == (
            "process_restart",
        )


@pytest.mark.asyncio
async def test_failed_terminal_transaction_never_claims_persistence(manager, monkeypatch):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)

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
            "SELECT t.content FROM tool_calls AS c "
            "JOIN entry_text AS t ON t.entry_key = c.result_entry_key ORDER BY c.call_key"
        ).fetchall() == [("first",), ("second",)]


@pytest.mark.asyncio
async def test_fork_ends_before_a_running_run_and_never_executes_it(manager):
    source = manager.create("coder")
    settled = source.start_run("settled")
    settled.append(ChatMessage.user("question"))
    complete_run(settled, _summary("settled"))
    in_flight = source.start_run("in-flight")
    in_flight.append(ChatMessage.assistant(model="test", content="partial"))

    fork = await manager.fork(source.address)
    inherited = [(message.role, message.content) for message in fork.load_active()]
    assert inherited[0] == ("user", "question")
    assert [role for role, _ in inherited] == ["user", "run_summary"]
    assert fork.load() == []

    manager.recover_interrupted_runs()
    assert source.find_run_summary(run_id="in-flight").status == "interrupted"
    assert fork.find_run_summary(run_id="in-flight") is None

    # Deleting the source gives the fork its own copy; a copied Run never runs again.
    manager.delete(source.address)
    assert [(message.role, message.content) for message in fork.load_active()] == inherited
    with sqlite3.connect(manager._store.path) as connection:
        assert connection.execute(
            "SELECT run_id, status, inherited, contributes_to_activity FROM runs"
        ).fetchall() == [("settled", "completed", 1, 0)]
    with pytest.raises(ChatSessionError, match="inherited Run"):
        fork.start_run("settled")


@pytest.mark.asyncio
async def test_settled_run_rejects_late_output_and_duplicate_completion(manager):
    session = manager.create("coder")
    runs = ChatRunManager(persistence=manager)
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


def test_snapshot_lists_page_runs_in_start_order(manager):
    session = manager.create("coder")
    for run_id in ("run-b", "run-a"):
        run = session.start_run(run_id)
        run.append(ChatMessage.user(run_id))
        run.append(ChatMessage.assistant(model="test", content="done"))
        complete_run(run, _summary(run_id))
    snapshot = session.read_chat_history_snapshot(limit=50)
    assert [(run["run_id"], run["complete"]) for run in snapshot.runs] == [
        ("run-b", True),
        ("run-a", True),
    ]
