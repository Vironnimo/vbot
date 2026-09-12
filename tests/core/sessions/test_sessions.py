"""Tests for sessions."""

from __future__ import annotations

import asyncio
import sqlite3

import core.sessions._store_codec as session_store_module
from core.chat import ChatMessage
from core.chat.content_blocks import FileMentionBlock, TextBlock
from core.chat.continuation import fold_continuation_records
from core.chat.messages import MessageSender, ToolCall, ToolCallRejection
from core.chat.output_files import AssistantFileReference
from core.sessions import (
    FORK_SOURCE_META_KEY,
    PROMPT_CACHE_AFFINITY_META_KEY,
)
from core.sessions import _store_values as store_values
from tests.core.sessions.sessions_test_support import (
    _address,
    _continuation_start,
)
from tests.core.sessions.sessions_test_support import (
    manager as manager,
)


def test_continuation_events_update_one_normalized_current_state(manager) -> None:
    session = manager.create("coder", session_id="continuation-state")
    session.append_continuation_records(
        [
            _continuation_start(),
            {
                "version": 1,
                "type": "stream_delta",
                "run_id": "run-one",
                "timestamp": "2026-08-31T12:00:01+00:00",
                "step": 1,
                "reasoning_delta": "first ",
                "content_delta": "partial ",
            },
            {
                "version": 1,
                "type": "stream_delta",
                "run_id": "run-one",
                "timestamp": "2026-08-31T12:00:02+00:00",
                "step": 1,
                "reasoning_delta": "second",
                "content_delta": "answer",
            },
            {
                "version": 1,
                "type": "tool_started",
                "run_id": "run-one",
                "timestamp": "2026-08-31T12:00:03+00:00",
                "tool_call_id": "call-one",
                "name": "bash",
            },
            {
                "version": 1,
                "type": "tool_result",
                "run_id": "run-one",
                "timestamp": "2026-08-31T12:00:04+00:00",
                "tool_call_id": "call-one",
                "name": "bash",
                "ok": True,
            },
            {
                "version": 1,
                "type": "run_interrupted",
                "run_id": "run-one",
                "timestamp": "2026-08-31T12:00:05+00:00",
                "cause": "user",
            },
        ]
    )

    state = fold_continuation_records(session.load_continuation_records())
    assert state is not None
    assert state.reasoning == "first second"
    assert state.partial_output == "partial answer"
    assert state.operations["call-one"]["status"] == "completed"
    assert state.cause == "user"
    with sqlite3.connect(manager._store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "continuation_records" not in tables
        assert connection.execute("SELECT COUNT(*) FROM continuations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM continuation_steps").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM continuation_operations").fetchone()[0] == 1


def test_fork_copies_history_but_not_activity_or_continuation(manager) -> None:
    source = manager.create("coder", session_id="source")
    source.append_many(
        [ChatMessage.user("hello"), ChatMessage.assistant(model="test", content="hi")]
    )
    source.append_continuation_record(_continuation_start())
    source_address = _address("coder", "source")
    manager.record_terminal_run(source_address, "run-1", "completed", "2026-08-29T12:00:00Z")

    forked = asyncio.run(manager.fork(source_address, target_agent_id="reviewer"))

    assert forked.load() == source.load()
    assert forked.load_continuation_records() == []
    assert manager.list_completion_activity("reviewer")[0]["has_unread_completion"] is False
    metadata = manager.get_metadata(forked.address)
    assert metadata[FORK_SOURCE_META_KEY]["session_id"] == "source"
    assert metadata[PROMPT_CACHE_AFFINITY_META_KEY] != manager.prompt_cache_affinity_id(
        source_address
    )


def test_role_specific_relational_message_storage_round_trips(
    manager, tmp_path, monkeypatch
) -> None:
    session = manager.create("coder", session_id="normalized")
    user = ChatMessage.user(
        [
            TextBlock(type="text", text="inspect the normalized store"),
            FileMentionBlock(
                type="file_mention",
                path="core/example.py",
                status="inlined",
                text="VALUE = 1",
                size_bytes=9,
            ),
        ],
        sender=MessageSender(id="human-one", display_name="Ada", role="admin"),
    )
    assistant = ChatMessage.assistant(
        model="provider/model",
        content="file:C:\\tmp\\result.txt",
        reasoning="reasoning text",
        reasoning_meta={"provider_state": {"opaque": True}},
        reasoning_scope="turn",
        reasoning_timing={
            "started_at": "2026-08-31T12:00:00+00:00",
            "completed_at": "2026-08-31T12:00:01+00:00",
            "duration_ms": 1000,
            "clock": "monotonic",
        },
        phase="analysis",
        usage={
            "input_tokens": 12,
            "output_tokens": 4,
            "cache_read_tokens": 2,
            "estimated": False,
            "provider_detail": {"tier": "test"},
        },
        tool_calls=[
            ToolCall(
                id="call-one",
                name="read",
                arguments={"path": "README.md"},
                rejection=ToolCallRejection(
                    code="policy",
                    message="not dispatched",
                    fingerprint="fingerprint",
                ),
            ),
            ToolCall(
                id="call-two",
                name="bash",
                arguments={"command": "echo ok"},
                argument_sequence_index=0,
                argument_sequence_length=2,
            ),
        ],
        interrupted=True,
        interruption_cause="user",
        output_files=[
            AssistantFileReference(
                line_index=0,
                path="C:\\tmp\\result.txt",
                start_index=0,
                end_index=len("file:C:\\tmp\\result.txt"),
            )
        ],
    )
    tool = ChatMessage.tool(
        tool_call_id="call-one",
        name="read",
        content=(
            '{"ok":false,"error":{"code":"denied","message":"not available",'
            '"retryable":false,"attempts_made":2},"data":null,"artifacts":[]}'
        ),
        timing={
            "started_at": "2026-08-31T12:00:01+00:00",
            "completed_at": "2026-08-31T12:00:02+00:00",
            "duration_ms": 1000,
            "clock": "monotonic",
        },
        tool_display={"version": 1, "summary": "read README"},
    )
    run_summary = ChatMessage.run_summary(
        run_id="run-one",
        work_id="work-one",
        status="interrupted",
        timing={
            "started_at": "2026-08-31T12:00:00+00:00",
            "completed_at": "2026-08-31T12:00:03+00:00",
            "duration_ms": 3000,
            "clock": "monotonic",
        },
        iteration_count=2,
        change_stats={
            "files": 1,
            "added": 3,
            "removed": 1,
            "paths": ["core/example.py"],
            "source": "git",
        },
    )

    session.append_many([user, assistant, tool, run_summary])

    assert session.load() == [user, assistant, tool, run_summary]
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        assert connection.execute(
            "SELECT content FROM messages WHERE message_id = ?", (tool.id,)
        ).fetchone() == (None,)
        assert connection.execute(
            """
            SELECT result_content, result_ok, error_code, error_message,
                   error_retryable, error_attempts_made, data_json, artifacts_json
            FROM tool_messages
            """
        ).fetchone() == (
            tool.content,
            0,
            "denied",
            "not available",
            0,
            2,
            None,
            "[]",
        )

    original_message_from_row = session_store_module.message_from_row

    def fail_message_reconstruction(_row):
        raise AssertionError("Fork must copy normalized rows without reconstructing Messages")

    monkeypatch.setattr(session_store_module, "message_from_row", fail_message_reconstruction)
    forked = asyncio.run(manager.fork(session.address, target_agent_id="reviewer"))
    monkeypatch.setattr(session_store_module, "message_from_row", original_message_from_row)

    assert forked.load() == [user, assistant, tool, run_summary]
    fork_hits = manager.fts_search(
        "normalized",
        project_id=forked.address.project_id,
        agent_id=forked.address.agent_id,
        session_id=forked.address.session_id,
    )
    assert [hit[1] for hit in fork_hits] == [user.id]


def test_session_list_order_queries_use_declared_indexes(manager, tmp_path) -> None:
    manager.create("coder", session_id="one", project_id=None)
    manager.create("reviewer", session_id="two", project_id="project")

    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        scoped_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT session_id FROM sessions "
                "WHERE status = 'live' AND project_id = ? AND agent_id = ? "
                "ORDER BY active_sort DESC, session_id LIMIT 20",
                ("", "coder"),
            )
        )
        global_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN WITH candidates AS ("
                "SELECT session_id, active_sort, project_id, agent_id FROM sessions "
                "WHERE status = 'live' AND ((project_id = ? AND agent_id = ?) "
                "OR (project_id = ? AND agent_id = ?)) "
                "AND (list_visibility_mask & ?) = 0) "
                "SELECT * FROM candidates ORDER BY active_sort DESC, project_id, agent_id, "
                "session_id LIMIT 20",
                (
                    "",
                    "coder",
                    "project",
                    "reviewer",
                    store_values._LIST_VISIBILITY_BACKGROUND,
                ),
            )
        )
        count_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM sessions "
                "WHERE status = 'live' AND ((project_id = ? AND agent_id = ?) "
                "OR (project_id = ? AND agent_id = ?)) "
                "AND (list_visibility_mask & ?) = 0",
                (
                    "",
                    "coder",
                    "project",
                    "reviewer",
                    store_values._LIST_VISIBILITY_BACKGROUND,
                ),
            )
        )

    assert "sessions_live_scope_order" in scoped_plan
    assert "USE TEMP B-TREE" not in scoped_plan
    assert "sessions_live_global_order" in global_plan
    assert "USE TEMP B-TREE" not in global_plan
    assert "COVERING INDEX sessions_live_scope_visibility" in count_plan
