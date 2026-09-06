"""Public Session facade tests for canonical SQLite persistence."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import core.sessions.store as session_store_module
from core.chat import ChatMessage, ChatSessionError
from core.chat.content_blocks import FileMentionBlock, TextBlock
from core.chat.continuation import fold_continuation_records
from core.chat.messages import MessageSender, ToolCall, ToolCallRejection
from core.chat.output_files import AssistantFileReference
from core.chat.usage import aggregate_session_usage
from core.runs import RunKind
from core.sessions import (
    FORK_SOURCE_META_KEY,
    PROMPT_CACHE_AFFINITY_META_KEY,
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SESSION_RUN_KINDS_META_KEY,
    ChatSessionManager,
    SessionAddress,
    SessionListFilters,
    active_session_messages,
    current_skill_activation_contents,
    editable_session_message_ids,
)


def _address(agent_id: str, session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


def _continuation_start() -> dict[str, object]:
    return {
        "version": 1,
        "type": "run_started",
        "checkpoint_id": "checkpoint-one",
        "run_id": "run-one",
        "origin_run_id": "run-one",
        "timestamp": "2026-08-31T12:00:00+00:00",
        "request": "continue this work",
    }


@pytest.fixture
def manager(tmp_path, current_session_store_template):
    shutil.copy2(current_session_store_template / "session-store.json", tmp_path)
    shutil.copy2(current_session_store_template / "sessions.db", tmp_path)
    sessions = ChatSessionManager(tmp_path)
    yield sessions
    sessions.close()


def test_create_append_and_load_use_a_canonical_database(manager, tmp_path) -> None:
    session = manager.create("coder", session_id="session-one")
    messages = [ChatMessage.user("hello"), ChatMessage.assistant(model="test", content="hi")]

    session.append_many(messages)

    assert session.load() == messages
    assert session.bookend_timestamps() == (messages[0].timestamp, messages[-1].timestamp)
    assert (tmp_path / "sessions.db").is_file()
    assert not list((tmp_path / "agents").glob("*/sessions/*.jsonl"))


def test_cursor_reads_only_messages_appended_after_the_snapshot(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.append(ChatMessage.user("first"))
    initial = session.load_since()
    assert initial is not None
    assert len(initial.messages) == 1

    assert session.load_since(initial.cursor) is not None
    session.append(ChatMessage.assistant(model="test", content="second"))
    appended = session.load_since(initial.cursor)
    assert appended is not None
    assert [message.content for message in appended.messages] == ["second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("project_id", [None, "project-one"])
async def test_reflection_runs_restore_only_own_review_summaries(
    manager, monkeypatch, status, project_id
):
    source = manager.create("coder", session_id="source", project_id=project_id)

    def summary(run_id, result):
        return ChatMessage.run_summary(
            run_id=run_id,
            status=result,
            iteration_count=1,
            timing={
                "started_at": "2026-09-05T10:00:00+00:00",
                "completed_at": "2026-09-05T10:00:01+00:00",
                "duration_ms": 1000,
            },
        )

    source.append(summary("inherited", "completed"))
    fork = await manager.fork(
        source.address,
        target_project_id=project_id,
        strip_meta_keys=SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    )
    manager.record_run_kind(fork.address, RunKind.MEMORY_REFLECTION)
    # An admitted fork with only copied summaries must not fabricate a result.
    assert source.reflection_runs() == []
    fork.append(summary("review", status))
    # Later user work inside the review Session must not replace its review result.
    manager.record_run_kind(fork.address, RunKind.USER)
    fork.append(summary("later-user", "completed"))
    other = manager.create("coder", session_id="other", project_id=project_id)
    other_fork = await manager.fork(
        other.address,
        target_project_id=project_id,
        strip_meta_keys=SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    )
    manager.record_run_kind(other_fork.address, RunKind.SKILL_REFLECTION)
    other_fork.append(summary("other-review", "completed"))
    other_scope = manager.create("coder", session_id="source", project_id="different-project")

    def forbid_history(*args, **kwargs):
        raise AssertionError("Reflection recovery must not reconstruct chat content")

    monkeypatch.setattr(session_store_module, "message_from_row", forbid_history)
    assert source.reflection_runs() == [
        {
            "session_id": fork.id,
            "run_id": "review",
            "status": status,
            "started_at": "2026-09-05T10:00:00+00:00",
            "run_kind": "memory_reflection",
        }
    ]
    assert other_scope.reflection_runs() == []
    await manager.archive(fork.address)
    assert source.reflection_runs() == []


def test_metadata_activity_and_continuation_change_state_not_history(manager) -> None:
    address = _address("coder", "session-one")
    session = manager.create("coder", session_id=address.session_id)
    session.append(ChatMessage.user("hello"))
    revision = manager.history_revision(address)

    manager.set_metadata(address, {"project": "vbot"})
    manager.record_run_kind(address, RunKind.USER)
    manager.record_terminal_run(address, "run-1", "completed", "2026-08-29T12:00:00Z")
    session.append_continuation_records([_continuation_start()])

    assert manager.history_revision(address) == revision
    assert manager.get_metadata(address)["project"] == "vbot"
    assert manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY] == [RunKind.USER.value]
    continuation = fold_continuation_records(session.load_continuation_records())
    assert continuation is not None
    assert continuation.checkpoint_id == "checkpoint-one"
    assert manager.mark_terminal_run_read(address, "wrong")["marked_read"] is False
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is True


def test_listable_metadata_is_normalized_out_of_open_ended_metadata(manager) -> None:
    address = _address("coder", "normalized-metadata")
    manager.create(address.agent_id, session_id=address.session_id)
    metadata = {
        "title": "Release planning",
        "auto_title": "Automatic title",
        "source_channel_id": "tg-main",
        "platform": "telegram",
        "platform_conv_id": "chat-42",
        "is_subagent_session": True,
        "subagent_parent": {"agent_id": "parent", "session_id": "root"},
        "fork_source": {"agent_id": "coder", "session_id": "source"},
        "run_kinds": ["subagent"],
        "compaction_policy": {"enabled": False},
        "pinned_working_project_context": "x" * 100_000,
    }

    manager.set_metadata(address, metadata)

    assert manager.get_metadata(address) == metadata
    with sqlite3.connect(manager._store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM sessions WHERE agent_id = ? AND session_id = ?",
            (address.agent_id, address.session_id),
        ).fetchone()
    assert row is not None
    residual = json.loads(row["metadata_json"])
    assert residual == {"pinned_working_project_context": "x" * 100_000}
    assert row["title"] == "Release planning"
    assert json.loads(row["subagent_parent_json"])["session_id"] == "root"
    assert json.loads(row["run_kinds_json"]) == ["subagent"]


def test_session_list_page_is_bounded_filtered_and_keeps_required_session(manager) -> None:
    normal_ids: list[str] = []
    for index in range(40):
        session_id = f"normal-{index:02d}"
        address = _address("coder", session_id)
        manager._store.create(address, created_at=f"2026-08-01T12:{index:02d}:00+00:00")
        manager.set_metadata(
            address,
            {
                "title": f"Normal {index}",
                "run_kinds": ["user"],
                "pinned_skill_catalog": "large" * 10_000,
            },
        )
        normal_ids.append(session_id)
    hidden = _address("coder", "cron-hidden")
    manager._store.create(hidden, created_at="2026-08-01T00:00:00+00:00")
    manager.set_metadata(hidden, {"run_kinds": ["cron"]})

    first = manager.list_summaries_page(
        [(None, "coder")],
        limit=35,
        filters=SessionListFilters(
            include_subagents=False,
            include_memory_reflections=False,
            include_skill_reflections=False,
            include_cron=False,
        ),
        required_address=hidden,
    )

    assert len(first.sessions) == 36
    assert first.total_count == 41
    assert first.next_cursor is not None
    assert first.sessions[0]["id"] == "normal-39"
    assert first.sessions[-1]["id"] == hidden.session_id
    assert all("pinned_skill_catalog" not in summary for summary in first.sessions)
    assert all(
        set(summary)
        <= {
            "id",
            "project_id",
            "agent_id",
            "created_at",
            "last_active_at",
            "title",
            "run_kinds",
            "latest_completion_run_id",
            "has_unread_completion",
            "unread_run_id",
            "unread_run_status",
            "unread_run_at",
        }
        for summary in first.sessions
    )

    second = manager.list_summaries_page(
        [(None, "coder")],
        limit=20,
        cursor=first.next_cursor,
        filters=SessionListFilters(
            include_subagents=False,
            include_memory_reflections=False,
            include_skill_reflections=False,
            include_cron=False,
        ),
        required_address=hidden,
    )
    paged_ids = {summary["id"] for summary in (*first.sessions, *second.sessions)}
    assert paged_ids == {*normal_ids, hidden.session_id}
    assert second.next_cursor is None


def test_completion_activity_projection_reads_only_activity_columns(manager) -> None:
    address = _address("coder", "activity-projection")
    manager.create(address.agent_id, session_id=address.session_id)
    manager.set_metadata(address, {"pinned_memory_files": "large" * 10_000})
    manager.record_terminal_run(address, "run-1", "completed", "2026-08-29T12:00:00Z")

    rows = manager._store.list_activity_rows(None, address.agent_id)

    assert set(rows[0].keys()) == {
        "session_id",
        "latest_completion_run_id",
        "latest_completion_status",
        "latest_completion_at",
        "read_completion_run_id",
    }
    assert manager.list_completion_activity(address.agent_id)[0]["unread_run_id"] == "run-1"


def test_session_list_cursor_is_stable_when_a_newer_session_is_inserted(manager) -> None:
    for index in range(4):
        manager._store.create(
            _address("coder", f"existing-{index}"),
            created_at=f"2026-08-01T00:0{index}:00+00:00",
        )
    first = manager.list_summaries_page([(None, "coder")], limit=2)
    assert first.next_cursor is not None

    manager._store.create(
        _address("coder", "inserted-newer"),
        created_at="2026-08-01T01:00:00+00:00",
    )
    second = manager.list_summaries_page(
        [(None, "coder")],
        limit=2,
        cursor=first.next_cursor,
    )

    assert [summary["id"] for summary in first.sessions] == ["existing-3", "existing-2"]
    assert [summary["id"] for summary in second.sessions] == ["existing-1", "existing-0"]


def test_session_list_filters_execution_categories_in_sql(manager) -> None:
    metadata_by_session = {
        "ordinary": {"run_kinds": ["user"]},
        "subagent": {"is_subagent_session": True, "run_kinds": ["subagent"]},
        "memory": {"run_kinds": ["memory_reflection"]},
        "skill": {"run_kinds": ["skill_reflection"]},
        "reflection": {"run_kinds": ["reflection"]},
        "cron": {"run_kinds": ["cron"]},
        "mixed": {"run_kinds": ["cron", "memory_reflection"]},
        "channel-cron": {
            "run_kinds": ["cron"],
            "platform": "telegram",
            "platform_conv_id": "chat-1",
        },
        "unknown-kind": {"run_kinds": ["future_kind"]},
    }
    for index, (session_id, metadata) in enumerate(metadata_by_session.items()):
        address = _address("coder", session_id)
        manager._store.create(
            address,
            created_at=f"2026-08-01T00:0{index}:00+00:00",
        )
        manager.set_metadata(address, metadata)

    def listed(filters: SessionListFilters) -> set[str]:
        return {
            summary["id"]
            for summary in manager.list_summaries_page(
                [(None, "coder")], limit=100, filters=filters
            ).sessions
        }

    hidden = SessionListFilters(False, False, False, False)
    assert listed(hidden) == {"ordinary", "channel-cron", "unknown-kind"}
    assert listed(SessionListFilters(True, False, False, False)) == {
        "ordinary",
        "subagent",
        "channel-cron",
        "unknown-kind",
    }
    assert listed(SessionListFilters(False, True, False, False)) == {
        "ordinary",
        "memory",
        "reflection",
        "channel-cron",
        "unknown-kind",
    }
    assert listed(SessionListFilters(False, False, True, False)) == {
        "ordinary",
        "skill",
        "reflection",
        "channel-cron",
        "unknown-kind",
    }
    assert listed(SessionListFilters(False, True, False, True)) == {
        "ordinary",
        "memory",
        "reflection",
        "cron",
        "mixed",
        "channel-cron",
        "unknown-kind",
    }


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
                    session_store_module._LIST_VISIBILITY_BACKGROUND,
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
                    session_store_module._LIST_VISIBILITY_BACKGROUND,
                ),
            )
        )

    assert "sessions_live_scope_order" in scoped_plan
    assert "USE TEMP B-TREE" not in scoped_plan
    assert "sessions_live_global_order" in global_plan
    assert "USE TEMP B-TREE" not in global_plan
    assert "COVERING INDEX sessions_live_scope_visibility" in count_plan


def test_move_updates_the_composite_address_without_losing_history(manager) -> None:
    source = manager.create("coder", session_id="session-one")
    message = ChatMessage.user("hello")
    source.append(message)
    target = _address("reviewer", source.id, "project-a")

    moved = asyncio.run(manager.move(source.address, target))

    assert not manager.exists(source.address)
    assert manager.exists(target)
    assert moved.address == target
    assert moved.load() == [message]


def test_move_reads_and_transforms_metadata_inside_its_writer_transaction(
    manager, monkeypatch
) -> None:
    source = manager.create("coder", session_id="session-one")
    target = _address("reviewer", source.id, "project-a")
    entered_store = threading.Event()
    original_move = manager._store.move

    def observed_move(*args, **kwargs):
        entered_store.set()
        return original_move(*args, **kwargs)

    monkeypatch.setattr(manager._store, "move", observed_move)
    writer = sqlite3.connect(manager._store.path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 "
            "WHERE agent_id = ? AND session_id = ? AND status = 'live'",
            (json.dumps({"title": "latest title"}), source.address.agent_id, source.id),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: asyncio.run(manager.move(source.address, target)))
            assert entered_store.wait(timeout=5)
            writer.commit()
            moved = future.result(timeout=5)
    finally:
        writer.close()

    assert manager.get_metadata(moved.address)["title"] == "latest title"


def test_fork_reads_and_transforms_metadata_inside_its_writer_transaction(
    manager, monkeypatch
) -> None:
    source = manager.create("coder", session_id="session-one")
    source.append(ChatMessage.user("hello"))
    entered_store = threading.Event()
    original_fork = manager._store.fork

    def observed_fork(*args, **kwargs):
        entered_store.set()
        return original_fork(*args, **kwargs)

    monkeypatch.setattr(manager._store, "fork", observed_fork)
    writer = sqlite3.connect(manager._store.path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 "
            "WHERE agent_id = ? AND session_id = ? AND status = 'live'",
            (json.dumps({"title": "latest title"}), source.address.agent_id, source.id),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: asyncio.run(manager.fork(source.address)))
            assert entered_store.wait(timeout=5)
            writer.commit()
            forked = future.result(timeout=5)
    finally:
        writer.close()

    metadata = manager.get_metadata(forked.address)
    assert metadata["title"] == "latest title"
    assert metadata[FORK_SOURCE_META_KEY]["message_count"] == 1
    assert forked.load() == source.load()


def test_archive_hides_session_until_explicit_restore(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    asyncio.run(manager.archive(address))

    assert manager.exists(address) is False
    assert manager.list("coder") == []
    with pytest.raises(ChatSessionError, match="does not exist"):
        manager.get(address)
    manager.restore(address)
    assert manager.exists(address) is True


def test_archived_address_can_start_a_fresh_generation(manager) -> None:
    address = _address("coder", "session-one")
    original = manager.create("coder", session_id=address.session_id)
    original.append(ChatMessage.user("old"))
    original_cursor = original.load_since().cursor
    asyncio.run(manager.archive(address))

    replacement = manager.get_or_create(address)
    replacement.append(ChatMessage.user("new"))

    assert [message.content for message in replacement.load()] == ["new"]
    assert replacement.load_since(original_cursor) is None
    with pytest.raises(ChatSessionError, match="live session already exists"):
        manager.restore(address)


def test_repeated_message_ids_are_preserved_in_sequence_order(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="checkpoint",
        projection=[],
        compacted_token_count=1,
        policy="automatic",
        strategy="summary",
    )

    session.append_many([checkpoint, checkpoint])

    assert session.load() == [checkpoint, checkpoint]


def test_chat_history_sql_usage_matches_canonical_python_aggregation(manager) -> None:
    session = manager.create("coder", session_id="usage-projection")
    messages = [
        ChatMessage.assistant(
            model="test",
            content="measured",
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 60,
                "cache_write_tokens": 5,
                "reasoning_tokens": 7,
            },
        ),
        ChatMessage.assistant(
            model="test",
            content="estimated input",
            usage={
                "input_tokens": 80,
                "output_tokens": 10,
                "input_tokens_estimated": True,
                "output_tokens_estimated": False,
            },
        ),
        ChatMessage.assistant(
            model="test",
            content="estimated output",
            usage={
                "input_tokens": 50,
                "output_tokens": 12,
                "cache_read_tokens": 25,
                "input_tokens_estimated": False,
                "output_tokens_estimated": True,
            },
        ),
    ]
    session.append_many(messages)

    snapshot = session.read_chat_history_snapshot(
        limit=1,
        excluded_roles=("note", "history_edit"),
    )

    assert snapshot.session_usage == aggregate_session_usage(messages)


def test_concurrent_metadata_mutations_do_not_overwrite_each_other(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(manager.record_run_kind, address, RunKind.USER),
            pool.submit(manager.record_run_kind, address, RunKind.REFLECTION),
        ]
        for future in futures:
            future.result()

    assert set(manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY]) == {
        RunKind.USER.value,
        RunKind.REFLECTION.value,
    }


def test_two_managers_append_concurrently_without_losing_messages(tmp_path) -> None:
    first = ChatSessionManager(tmp_path)
    second = ChatSessionManager(tmp_path)
    address = _address("coder", "session-one")
    first.create("coder", session_id=address.session_id)
    barrier = threading.Barrier(2)

    def append(manager: ChatSessionManager, prefix: str) -> None:
        session = manager.get(address)
        barrier.wait()
        for index in range(40):
            session.append(ChatMessage.user(f"{prefix}-{index}"))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(append, first, "first"),
                pool.submit(append, second, "second"),
            ]
            for future in futures:
                future.result()

        contents = [message.content for message in first.get(address).load()]
        assert len(contents) == 80
        assert set(contents) == {
            *(f"first-{index}" for index in range(40)),
            *(f"second-{index}" for index in range(40)),
        }
    finally:
        second.close()
        first.close()


def test_two_managers_get_or_create_one_live_generation(tmp_path) -> None:
    first = ChatSessionManager(tmp_path)
    second = ChatSessionManager(tmp_path)
    address = _address("coder", "session-one")
    barrier = threading.Barrier(2)

    def create(manager: ChatSessionManager):
        barrier.wait()
        return manager.get_or_create(address)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            handles = [
                pool.submit(create, first),
                pool.submit(create, second),
            ]
            assert [future.result().address for future in handles] == [address, address]
        assert [session.id for session in first.list("coder")] == [address.session_id]
    finally:
        second.close()
        first.close()


def test_callback_failure_does_not_turn_a_committed_title_into_an_error(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    def fail(_address: SessionAddress) -> None:
        raise RuntimeError("observer failed")

    manager.add_title_changed_callback(fail)

    assert manager.set_title(address, "Persisted") == "Persisted"
    assert manager.get_metadata(address)["title"] == "Persisted"


def test_deferred_notes_keep_their_existing_ordering(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.begin_defer_notes()
    session.add_note("first")
    session.add_note("second")

    session.flush_deferred_notes()

    assert [message.content for message in session.load()] == ["first", "second"]
    assert [message.content for message in session.drain_pending_notes()] == ["first", "second"]


def test_history_edits_and_skill_cache_preserve_chat_semantics(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    user = ChatMessage.user("first")
    replacement = ChatMessage.user("replacement")
    session.append_many([user, ChatMessage.history_edit(user.id), replacement])

    assert active_session_messages(session.load()) == [replacement]
    assert editable_session_message_ids(session.load()) == frozenset({replacement.id})
    assert current_skill_activation_contents(session.load()) == {}


def test_write_lock_is_reentrant_for_child_tasks(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    async def scenario() -> None:
        async with manager.write_lock(address):

            async def child() -> None:
                async with manager.write_lock(address):
                    manager.get(address).append(ChatMessage.note("child"))

            await asyncio.create_task(child())

    asyncio.run(scenario())
    assert [message.content for message in manager.get(address).load()] == ["child"]


@pytest.mark.parametrize("agent_id", ["", "../outside", "agent name"])
def test_create_rejects_invalid_agent_ids(manager, agent_id) -> None:
    with pytest.raises(ChatSessionError):
        manager.create(agent_id, session_id="session-one")


@pytest.mark.parametrize("session_id", ["", "../outside", "name.jsonl", ".hidden"])
def test_create_rejects_invalid_session_ids(manager, session_id) -> None:
    with pytest.raises(ChatSessionError):
        manager.create("coder", session_id=session_id)


@pytest.mark.asyncio
async def test_generated_session_ids_skip_live_and_archived_collisions(manager, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2, 1, 2, 3))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first = manager.create("agent")
    second = manager.create("agent")
    await manager.archive(_address("agent", first.id))
    third = manager.create("agent")
    assert (first.id, second.id, third.id) == (
        "ses_000000000001",
        "ses_000000000002",
        "ses_000000000003",
    )
    assert manager.get(_address("agent", second.id)).id == second.id


@pytest.mark.asyncio
async def test_fork_allocates_a_short_id_without_reusing_an_archived_address(manager, monkeypatch):
    from core.utils import ids

    source = manager.create("agent", "ses_000000000001")
    manager.create("agent", "ses_000000000002")
    await manager.archive(_address("agent", "ses_000000000002"))
    values = iter((1, 2, 3))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    forked = await manager.fork(_address("agent", source.id))
    assert forked.id == "ses_000000000003"
    assert manager.get(_address("agent", source.id)).id == source.id


def test_parallel_generated_sessions_claim_ids_in_the_write_transaction(manager, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(lambda _: manager.create("agent"), range(2)))
    assert {session.id for session in sessions} == {"ses_000000000001", "ses_000000000002"}
