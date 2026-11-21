"""What forks share, what deleting an ancestor copies, and what a delete removes."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.chat.messages import GoalEvent, MessageSender, ModelFallback, ToolCall, WidgetState
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
from core.sessions.errors import SessionNotFoundError
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
        thinking_effort="high",
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
async def test_side_chat_freezes_live_history_and_finds_the_latest_live_fork(manager):
    source = manager.create("coder", project_id="work")
    earlier = ChatMessage.user("Settled question")
    source.append(earlier)
    active = source.start_run("live")
    assistant = ChatMessage.assistant(
        model="test",
        content="Unique active output",
        usage={"input_tokens": 20, "output_tokens": 8},
        tool_calls=[ToolCall(id="call", name="read", arguments={})],
    )
    active.append(assistant)

    generation = source.generation_id
    assert await manager.latest_side_chat(source.address) == (generation, None)
    with pytest.raises(SessionNotFoundError):
        await manager.latest_side_chat(SessionAddress(None, "coder", source.id))
    owned = SessionAddress(None, "workflow", "participant")
    manager.create_bound_temporary_session(
        owned, owner_name="workflow", group_id="group", participant_id="one", config={}
    )
    with pytest.raises(ChatSessionError):
        await manager.latest_side_chat(owned)
    side = await manager.fork(source.address, side_chat_note="Context")
    assert await manager.latest_side_chat(source.address) == (side.generation_id, side.address)
    (note,) = side.load()
    assert note.role == "note" and note.content == "Context"
    assert manager.get_metadata(side.address)["side_chat"] is True
    assert side.address.project_id == source.address.project_id
    assert [m.id for m in side.load_active()] == [earlier.id, assistant.id, note.id]
    assert side.run_records() == ()
    with pytest.raises(ChatSessionError, match="inherited Run"):
        side.start_run("live")
    active.assistant_message_id = assistant.id
    active.append(ChatMessage.tool(tool_call_id="call", name="read", content="Later result"))
    complete_run(active, _summary("live"))
    whole = side.read_chat_history_snapshot(limit=50)
    assert [r["status"] for r in whole.runs] == ["interrupted"]
    assert [m.id for m in whole.page.messages] == [earlier.id, assistant.id, note.id]
    assert earlier.id in whole.page.editable_message_ids
    hits = manager.search_messages(
        "Unique active", project_id="work", agent_id="coder", session_id=side.id
    ).hits
    assert [hit.message_id for hit in hits] == [assistant.id]
    assert manager.fts_health().state == "healthy"

    question = ChatMessage.user("Side question")
    reply = ChatMessage.assistant(model="test", content="Side answer")
    side.append_many([question, reply])
    page = side.read_chat_history_snapshot(limit=1, include_inherited=False)
    assert page.page.has_more and [m.id for m in page.page.messages] == [reply.id]
    before = side.read_chat_history_snapshot(
        limit=2, before=page.page.before_cursor, include_inherited=False
    )
    assert not before.page.has_more
    assert [m.id for m in before.page.messages] == [note.id, question.id]
    assert before.page.editable_message_ids == {question.id}
    assert page.context_messages == side.read_chat_history_snapshot(limit=1).context_messages
    assert side.read_chat_history_snapshot(limit=50).page.record_sequences == tuple(range(5))

    later = await manager.fork(source.address, side_chat_note="Next context")
    assert await manager.latest_side_chat(source.address) == (later.generation_id, later.address)
    await manager.archive(later.address)
    assert await manager.latest_side_chat(source.address) == (side.generation_id, side.address)
    nested = await manager.fork(side.address, side_chat_note="Nested context")
    assert manager.get_metadata(nested.address)["side_chat"] is True
    assert [m.content for m in nested.load()] == ["Nested context"]
    assert "side_chat" not in manager.get_metadata(
        (await manager.fork(side.address, target_project_id="work")).address
    )
    side.apply_edit(earlier.id, [ChatMessage.user("Edit in the full fork")])
    assert source.load_active()[0].id == earlier.id
    with pytest.raises(ChatSessionError, match="same|source Agent"):
        await manager.fork(source.address, target_agent_id="other", side_chat_note="n")


@pytest.mark.asyncio
async def test_side_chat_keeps_session_settings_and_uses_its_first_own_input_for_titles(manager):
    source = manager.create(
        "coder",
        working_project_id="project",
        metadata={
            "title": "Parent",
            "auto_title": "Generated title",
            "auto_title_initialized": True,
            "fast_mode": True,
            "goal": {"objective": "Important task", "status": "active"},
            "agent_overrides": {
                "model": "other/model",
                "permission_rules": {"mode": "strict"},
                "read_folders": [{"computer_id": "local", "folder": "one"}],
            },
        },
    )
    source.append(ChatMessage.user("Parent input"))
    side = await manager.fork(source.address, side_chat_note="Branch context", title="")
    meta = manager.get_metadata(side.address)
    assert meta["fast_mode"] and meta["working_project_id"] == "project"
    assert meta["agent_overrides"] == {
        "model": "other/model",
        "permission_rules": {"mode": "strict"},
    }
    assert not {"title", "auto_title", "auto_title_initialized", "goal", "ephemeral"} & meta.keys()
    assert side.single_active_user_message() is None
    own = ChatMessage.user("Side input")
    side.append(own)
    seq, message = side.single_active_user_message()
    assert message.id == own.id
    manager.set_auto_title(side.address, "Own title", expected_first_user_sequence=seq)
    next_message = ChatMessage.user("New side input")
    side.apply_edit(own.id, [next_message])
    assert [m.content for m in side.load_active() if m.role == "note"] == ["Branch context"]
    assert side.single_active_user_message()[1].id == next_message.id
    assert "auto_title" not in manager.get_metadata(side.address)
    with pytest.raises(ChatSessionError, match="managed"):
        manager.set_metadata(side.address, {"side_chat": "invalid"})
    manager.set_metadata(side.address, {"title": "Named"})
    assert manager.get_metadata(side.address)["side_chat"] is True


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
    # A review fork follows its source: the delete takes it first, so it gets no copies.
    review = await manager.fork(source.address, run_kind=RunKind.MEMORY_REFLECTION)
    review.append(ChatMessage.user("reviewed"))
    # Later source work and an edit of the forked question change no fork.
    source.append(ChatMessage.user("source only"))
    question = source.load_active()[0]
    source.apply_edit(question.id, [ChatMessage.user("rewritten")])
    inherited = source.load()[:6]
    assert child.load_active() == grandchild.load_active() == inherited
    revisions = [history_revision(manager, fork.address) for fork in (child, grandchild)]
    before_materialization = child.load_since()
    assert before_materialization is not None
    assert before_materialization.active_owned == (False,) * len(inherited)

    manager.delete(source.address)

    assert not manager.exists(review.address)
    assert child.load_active() == grandchild.load_active() == inherited
    assert child.load() == grandchild.load() == []
    assert child.load_since(before_materialization.cursor) is None
    materialized = child.load_since()
    assert materialized is not None
    assert materialized.active_owned == (True,) * len(inherited)
    assert materialized.inherited_count == len(inherited)
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
    _tool_run(source, "run-one", "retained text")
    inherited = source.load_active()
    tables = (
        "entries",
        "runs",
        "run_change_paths",
        "entry_text",
        "tool_calls",
        "tool_call_payloads",
        "tool_result_payloads",
    )

    def add_newer_fields(connection: sqlite3.Connection) -> None:
        for table in tables:
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
    entry = await manager.archive(child.address)
    ledger = manager.archive_ledger
    ledger.begin_purge(entry.entry_id)
    assert ledger.purge_next_session(entry.entry_key)
    ledger.finish_purge(entry.entry_key)

    assert grandchild.load_active() == inherited
    with manager.database.read() as connection:
        for table in tables:
            assert _rows(
                connection, f'SELECT DISTINCT future_text, future_count, "references" FROM {table}'
            ) == [(f"durable {table}", 42, f"retained {table}")]
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
            ChatMessage.note("", goal_event=GoalEvent("paused", reason="user")),
            ChatMessage.note("Values", widget_states=[WidgetState("res_a", "Spacing", {"a": 1})]),
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
        "note_goal_events",
        "note_widget_states",
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
