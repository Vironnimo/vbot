"""What forks share, what deleting an ancestor copies, and what a delete removes."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.chat.messages import MessageSender, ModelFallback, ToolCall
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
    """Write one completed Run with a Model fallback, a failed Tool call, and a result payload."""
    run = session.start_run(run_id)
    assistant = ChatMessage.assistant(
        model="model",
        content=None,
        reasoning=f"{text} reasoning",
        usage={"input_tokens": 10, "output_tokens": 2},
        tool_calls=[ToolCall(id=f"call-{run_id}", name="read", arguments={"path": "x"})],
    )
    fallback = ChatMessage.note(
        "Model primary unavailable.",
        model_fallback=ModelFallback(from_model="primary", to_model="model"),
    )
    run.append_many([ChatMessage.user(f"{text} question"), fallback, assistant])
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
async def test_a_fork_shares_history_until_its_ancestor_is_deleted(
    manager: ChatSessionManager,
) -> None:
    source = manager.create("agent", session_id="source")
    _tool_run(source, "run-one", "needle")
    child = await manager.fork(source.address)
    grandchild = await manager.fork(child.address)
    with sqlite3.connect(manager._store.path) as connection:
        shared = _rows(connection, "SELECT COUNT(*) FROM entries")
    assert shared == [(6,)]
    # Later source work and an edit of the forked question change no fork.
    source.append(ChatMessage.user("source only"))
    question = source.load_active()[0]
    source.apply_edit(question.id, [ChatMessage.user("rewritten")])
    inherited = source.load()[:6]
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
            # Each fork holds its own current copy of the six inherited entries.
            assert _rows(
                connection,
                "SELECT seq, role FROM entries WHERE session_key = ? "
                "AND superseded_at_seq IS NULL ORDER BY seq",
                key,
            ) == [
                (0, "user"),
                (1, "note"),
                (2, "assistant"),
                (3, "tool"),
                (4, "assistant"),
                (5, "run_summary"),
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
            ) == [("failed", 0, "boom", key, "tool", "res_run-one", '{"text": "needle payload"}')]
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
        (fork.id, role) for fork in (child, grandchild) for role in ("user", "tool", "assistant")
    )


@pytest.mark.asyncio
async def test_ancestor_delete_preserves_additive_columns(manager: ChatSessionManager) -> None:
    """Columns a newer vBot added survive every materialized copy."""
    source = manager.create("agent", session_id="source")
    run = source.start_run("run-one")
    run.append(ChatMessage.user("retained text"))
    complete_run(run, _summary("run-one"))
    inherited = source.load_active()

    def add_newer_fields(connection: sqlite3.Connection) -> None:
        for table in ("entries", "run_change_paths"):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN future_text TEXT")
            connection.execute(f'ALTER TABLE {table} ADD COLUMN "references" TEXT')
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN future_count INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                f"UPDATE {table} SET future_text = 'durable {table}', future_count = 42, "
                f"\"references\" = 'retained {table}'"
            )

    manager.database.write(add_newer_fields)
    child = await manager.fork(source.address)
    manager.delete(source.address)
    grandchild = await manager.fork(child.address)
    manager.delete(child.address)

    assert grandchild.load_active() == inherited
    with manager.database.read() as connection:
        assert _rows(
            connection, 'SELECT DISTINCT future_text, future_count, "references" FROM entries'
        ) == [("durable entries", 42, "retained entries")]
        assert _rows(
            connection,
            'SELECT p.ordinal, p.path, p.future_text, p.future_count, p."references", '
            "r.run_id, r.inherited, s.session_id FROM run_change_paths AS p "
            "JOIN runs AS r ON r.run_key = p.run_key "
            "JOIN sessions AS s ON s.session_key = r.session_key",
        ) == [
            (
                0,
                "notes.md",
                "durable run_change_paths",
                42,
                "retained run_change_paths",
                "run-one",
                1,
                grandchild.id,
            )
        ]
        assert _rows(connection, "PRAGMA foreign_key_check") == []


def _unread_run_ids(manager: ChatSessionManager) -> dict[str, str | None]:
    rows = manager.list_completion_activity([(None, "agent")])[(None, "agent")]
    return {str(row["id"]): row["unread_run_id"] for row in rows}


@pytest.mark.asyncio
async def test_the_latest_completion_names_an_own_run_across_fork_and_delete(
    manager: ChatSessionManager,
) -> None:
    source = manager.create("agent", session_id="source")
    _tool_run(source, "run-one", "needle")
    fork = await manager.fork(source.address)
    # A fork starts without a completion; an inherited Run's id marks nothing read.
    assert _unread_run_ids(manager) == {"source": "run-one"}
    assert manager.mark_terminal_run_read(fork.address, "run-one")["marked_read"] is False
    _tool_run(fork, "fork-run", "fork")
    assert _unread_run_ids(manager) == {"source": "run-one", fork.id: "fork-run"}

    # Materializing the inherited Run gives it a new key in the fork; the
    # fork's completion still names its own Run.
    manager.delete(source.address)

    assert _unread_run_ids(manager) == {fork.id: "fork-run"}
    with sqlite3.connect(manager._store.path) as connection:
        assert _rows(
            connection,
            "SELECT r.run_id, r.inherited FROM sessions AS s "
            "JOIN runs AS r ON r.run_key = s.latest_completion_run_key "
            "AND r.session_key = s.session_key WHERE s.session_id = ?",
            fork.id,
        ) == [("fork-run", 0)]
        assert _rows(connection, "PRAGMA foreign_key_check") == []
    assert manager.mark_terminal_run_read(fork.address, "run-one")["marked_read"] is False
    assert manager.mark_terminal_run_read(fork.address, "fork-run")["marked_read"] is True
    assert _unread_run_ids(manager) == {fork.id: None}


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
    streaming = session.start_run(f"{session_id}-streaming")
    asyncio.run(
        streaming.append_stream_draft_async(
            model="test", reasoning_delta="plan", content_delta="partial"
        )
    )
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


def test_deleting_a_session_removes_exactly_what_it_owns(manager: ChatSessionManager) -> None:
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
    assert {
        "tool_result_payloads",
        "run_stream_drafts",
        "user_entry_senders",
        "note_model_fallbacks",
    } <= owned
    assert populated["prompt_blobs"] == before["prompt_blobs"] + 1

    manager.delete(removed.address)

    assert _table_counts(manager._store.path) == before
    with sqlite3.connect(manager._store.path) as connection:
        assert _rows(connection, "PRAGMA foreign_key_check") == []
        assert _rows(connection, "SELECT value_json FROM prompt_blobs ORDER BY blob_key") == [
            ('{"catalog":"shared"}',),
            ('{"files":"kept"}',),
        ]


@pytest.mark.asyncio
async def test_deleting_an_owner_group_removes_exactly_its_sessions(
    manager: ChatSessionManager,
) -> None:

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
