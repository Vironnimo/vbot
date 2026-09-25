"""What forks share, what deleting an ancestor copies, and what a delete removes."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.chat.messages import MessageSender, ToolCall
from core.chat.output_files import AssistantFileReference
from core.prompts.pinned_context import PINNED_MEMORY_FILES_SLOT, PINNED_SKILL_CATALOG_SLOT
from core.runs import Run, RunExecutionOwner, RunKind
from core.sessions import (
    FORK_SOURCE_META_KEY,
    ChatSession,
    ChatSessionManager,
    SeenSkillsUpdate,
    SessionAddress,
    ToolResultFacts,
)
from tests.core.sessions.history_fixtures import admit_run, complete_run, history_revision


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
        change_stats={"files": 1, "added": 2, "removed": 0, "paths": ["notes.md"]},
    )


def _tool_run(session: ChatSession, run_id: str, text: str) -> None:
    """Write one completed Run with a failed Tool call and an attached result payload."""
    run = session.start_run(run_id)
    assistant = ChatMessage.assistant(
        model="model",
        content=None,
        reasoning=f"{text} reasoning",
        usage={"input_tokens": 10, "output_tokens": 2},
        tool_calls=[ToolCall(id=f"call-{run_id}", name="read", arguments={"path": "x"})],
    )
    run.append_many([ChatMessage.user(f"{text} question"), assistant])
    run.assistant_message_id = assistant.id
    run.append_many(
        [ChatMessage.tool(tool_call_id=f"call-{run_id}", name="read", content=f"{text} result")],
        tool_results={f"call-{run_id}": ToolResultFacts("failed", False, "boom", True, 2)},
    )
    run.append(
        ChatMessage.assistant(
            model="model",
            content=f"{text} answer\nfile:notes.md",
            output_files=[
                AssistantFileReference(
                    line_index=1, path="notes.md", start_index=0, end_index=len("file:notes.md")
                )
            ],
        )
    )
    complete_run(run, _summary(run_id))
    with sqlite3.connect(session._store.path) as connection:
        connection.execute(
            "INSERT INTO tool_result_payloads (payload_id, call_key, owner_name, created_at, "
            "payload_json) SELECT ?, call_key, 'mcp', '2026-09-19T10:00:00.000000Z', ? "
            "FROM tool_calls WHERE call_id = ?",
            (f"res_{run_id}", f'{{"text": "{text} payload"}}', f"call-{run_id}"),
        )


def _rows(connection: sqlite3.Connection, sql: str, *params: object) -> list[tuple]:
    return [tuple(row) for row in connection.execute(sql, params).fetchall()]


@pytest.mark.asyncio
async def test_a_fork_shares_history_until_its_ancestor_is_deleted(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        source = manager.create("agent", session_id="source")
        _tool_run(source, "run-one", "needle")
        child = await manager.fork(source.address)
        grandchild = await manager.fork(child.address)
        with sqlite3.connect(manager._store.path) as connection:
            shared = _rows(connection, "SELECT COUNT(*) FROM entries")
        assert shared == [(5,)]
        # Later source work and an edit of the forked question change no fork.
        source.append(ChatMessage.user("source only"))
        question = source.load_active()[0]
        source.apply_edit(question.id, [ChatMessage.user("rewritten")])
        inherited = source.load()[:5]
        assert child.load_active() == grandchild.load_active() == inherited
        revisions = [history_revision(manager, fork.address) for fork in (child, grandchild)]

        manager.delete(source.address)

        assert child.load_active() == grandchild.load_active() == inherited
        assert child.load() == grandchild.load() == []
        assert [history_revision(manager, fork.address) for fork in (child, grandchild)] == [
            revision + 1 for revision in revisions
        ]
        with sqlite3.connect(manager._store.path) as connection:
            assert _rows(connection, "SELECT COUNT(*) FROM session_lineage") == [(0,)]
            assert _rows(connection, "PRAGMA foreign_key_check") == []
            for fork in (child, grandchild):
                key = _rows(
                    connection, "SELECT session_key FROM sessions WHERE session_id = ?", fork.id
                )[0][0]
                # Each fork holds its own current copy of the five inherited entries.
                assert _rows(
                    connection,
                    "SELECT seq, role FROM entries WHERE session_key = ? "
                    "AND superseded_at_seq IS NULL ORDER BY seq",
                    key,
                ) == [
                    (0, "user"),
                    (1, "assistant"),
                    (2, "tool"),
                    (3, "assistant"),
                    (4, "run_summary"),
                ]
                # The copied Tool call keeps its outcome and points at the copied result.
                assert _rows(
                    connection,
                    "SELECT c.status, c.result_ok, c.error_code, r.session_key, r.role, "
                    "p.payload_id, p.payload_json FROM tool_calls AS c "
                    "JOIN entries AS a ON a.entry_key = c.entry_key "
                    "JOIN entries AS r ON r.entry_key = c.result_entry_key "
                    "JOIN tool_result_payloads AS p ON p.call_key = c.call_key "
                    "WHERE a.session_key = ?",
                    key,
                ) == [
                    ("failed", 0, "boom", key, "tool", "res_run-one", '{"text": "needle payload"}')
                ]
                # The copied Run is inherited: it never runs, reports no activity,
                # and its summary entry is the copied one.
                assert _rows(
                    connection,
                    "SELECT r.run_id, r.status, r.inherited, r.contributes_to_activity, "
                    "e.session_key, e.role, (SELECT COUNT(*) FROM run_change_paths AS p "
                    "WHERE p.run_key = r.run_key) FROM runs AS r "
                    "JOIN entries AS e ON e.entry_key = r.end_entry_key WHERE r.session_key = ?",
                    key,
                ) == [("run-one", "completed", 1, 0, key, "run_summary", 1)]
            assert _rows(
                connection,
                "SELECT COUNT(*) FROM tool_result_payloads WHERE payload_id = ?",
                "res_run-one",
            ) == [(2,)]
        with pytest.raises(ChatSessionError, match="inherited Run"):
            child.start_run("run-one")
        # Provenance follows the direct source: the child's is gone, the grandchild's stays.
        assert FORK_SOURCE_META_KEY not in manager.get_metadata(child.address)
        assert manager.get_metadata(grandchild.address)[FORK_SOURCE_META_KEY]["session_id"] == (
            child.id
        )
        hits = manager.search_messages("needle", project_id=None, agent_id="agent").hits
        assert sorted((hit.address.session_id, hit.role) for hit in hits) == sorted(
            (fork.id, role)
            for fork in (child, grandchild)
            for role in ("user", "tool", "assistant")
        )
    finally:
        manager.close()


def _populate(manager: ChatSessionManager, session_id: str) -> ChatSession:
    """Write one Session that owns a row in every Session-owned table."""
    session = manager.create("agent", session_id=session_id)
    address = session.address
    manager.set_metadata(address, {"title": session_id, "custom": {"kept": True}})
    asyncio.run(admit_run(manager, address, RunKind.CRON))
    manager.record_seen_skills(address, SeenSkillsUpdate(baseline=("alpha",)))
    manager.ensure_prompt_pin(
        address, PINNED_SKILL_CATALOG_SLOT, {"catalog": "shared"}, lambda _pin: True
    )
    manager.ensure_prompt_pin(
        address, PINNED_MEMORY_FILES_SLOT, {"files": session_id}, lambda _pin: True
    )
    _tool_run(session, f"{session_id}-run", session_id)
    session.append_many(
        [
            ChatMessage.user(
                "from a member", sender=MessageSender(id="member", display_name="Member")
            ),
            ChatMessage.error("provider", "failed"),
            ChatMessage.compaction_checkpoint(
                summary="summary", projection=[], compacted_token_count=1
            ),
        ]
    )
    edited = ChatMessage.user("draft")
    session.append(edited)
    session.apply_edit(edited.id, [ChatMessage.user("final")])
    run_id = f"{session_id}-continued"
    continued = session.start_run(run_id)
    continued.append_continuation_records(
        [
            {
                "version": 1,
                "type": "run_started",
                "checkpoint_id": "checkpoint-one",
                "run_id": run_id,
                "origin_run_id": run_id,
                "timestamp": "2026-08-31T12:00:00+00:00",
                "request": "continue this work",
            },
            {
                "version": 1,
                "type": "stream_delta",
                "run_id": run_id,
                "step": 1,
                "content_delta": "partial",
            },
            {
                "version": 1,
                "type": "tool_started",
                "run_id": run_id,
                "tool_call_id": "operation",
                "name": "read",
            },
        ]
    )
    manager.record_terminal_run(address, f"{session_id}-run", "completed", "2026-09-19T10:00:01Z")
    return session


def _table_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'entries_fts%'"
            )
        ]
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
        for table in ("entries_fts", "entries_fts_trigram"):
            counts[f"{table} rows"] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}_docsize").fetchone()[0]
            )
    return counts


def test_deleting_a_session_removes_exactly_what_it_owns(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    try:
        _populate(manager, "kept")
        before = _table_counts(manager._store.path)
        removed = _populate(manager, "removed")
        populated = _table_counts(manager._store.path)
        # The second Session owns rows in every Session-owned table the first
        # one does, and shares one prompt blob with it.
        shared_tables = {"store_meta", "prompt_blobs"}
        owned = {
            table
            for table, count in before.items()
            if count and table not in shared_tables and not table.startswith("kernel_")
        }
        assert owned <= {table for table, count in populated.items() if count > before[table]}
        assert {"tool_result_payloads", "continuation_step_chunks", "user_entry_senders"} <= owned
        assert populated["prompt_blobs"] == before["prompt_blobs"] + 1

        manager.delete(removed.address)

        assert _table_counts(manager._store.path) == before
        with sqlite3.connect(manager._store.path) as connection:
            assert _rows(connection, "PRAGMA foreign_key_check") == []
            assert _rows(connection, "SELECT value_json FROM prompt_blobs ORDER BY blob_key") == [
                ('{"catalog":"shared"}',),
                ('{"files":"kept"}',),
            ]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_deleting_an_owner_group_removes_exactly_its_sessions(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)

    async def populate(group_id: str) -> None:
        address = SessionAddress(None, "participant", group_id)
        binding = manager.create_bound_temporary_session(
            address, owner_name="swarm", group_id=group_id, participant_id="peer", config={}
        )
        manager.set_temporary_group_title(owner_name="swarm", group_id=group_id, title=group_id)
        owner = RunExecutionOwner("swarm", group_id, "peer", binding.generation_id, "epoch")
        await manager.start_run(
            Run(
                run_id=f"{group_id}-run",
                agent_id=address.agent_id,
                session_id=address.session_id,
                execution_owner=owner,
                execution_input_id=f"{group_id}-input",
            )
        )
        manager.append_messages_with_receipts(
            address,
            generation_id=binding.generation_id,
            owner_name="swarm",
            messages=[ChatMessage.note(f"{group_id} delivery")],
            receipts=[(0, f"{group_id}-delivery", "hash", "note", "note")],
        )

    try:
        await populate("kept")
        before = _table_counts(manager._store.path)
        await populate("removed")
        populated = _table_counts(manager._store.path)
        assert {
            "temporary_session_bindings",
            "temporary_group_titles",
            "run_execution_owners",
            "session_delivery_receipts",
        } <= {table for table, count in populated.items() if count > before[table]}

        assert await manager.delete_temporary_group(owner_name="swarm", group_id="removed") == 1

        assert _table_counts(manager._store.path) == before
        with sqlite3.connect(manager._store.path) as connection:
            assert _rows(connection, "PRAGMA foreign_key_check") == []
    finally:
        manager.close()
