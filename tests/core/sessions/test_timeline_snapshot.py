"""Cursor and canonical Run identity contracts for accessor Timeline reads."""

from __future__ import annotations

import asyncio

import pytest

from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from tests.core.sessions.sessions_test_support import manager as manager


def read(session, **kwargs):
    return session.read_chat_history_snapshot(
        excluded_roles=("note", "history_edit"), **{"limit": 50, **kwargs}
    )


def summary(run_id):
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


def start(manager, session, run_id):
    return session.start_run(run_id)


def test_run_identity_survives_bounded_page_without_user_or_summary(manager):
    session = manager.create("coder")
    session = start(manager, session, "first")
    session.append_many(
        [
            ChatMessage.user("question"),
            ChatMessage.assistant(content="answer", model="test"),
            summary("first"),
        ]
    )
    session = start(manager, session, "automatic")
    session.append_many(
        [
            ChatMessage.note("trigger"),
            ChatMessage.assistant(content="prefix", model="test"),
            ChatMessage.assistant(content="tail", model="test"),
        ]
    )
    page = read(session, limit=1)
    assert page.page.record_sequences == (5,)
    assert page.page.record_run_ids == ("automatic",)
    session.append(summary("automatic"))
    delta = read(session, after=page.after_cursor)
    assert delta.incremental
    assert delta.page.record_sequences == (6,)
    assert delta.page.record_run_ids == ("automatic",)


def test_empty_runs_with_equal_start_sequence_do_not_claim_successor(manager):
    session = manager.create("coder")
    session = start(manager, session, "empty")
    session = start(manager, session, "successor")
    session.append(ChatMessage.assistant(content="output", model="test"))
    assert read(session).page.record_run_ids == ("successor",)


def test_summary_does_not_assign_previous_failed_run_to_successor(manager):
    session = manager.create("coder")
    session = start(manager, session, "missing-summary")
    session.append(ChatMessage.assistant(content="partial", model="test"))
    session = start(manager, session, "successor")
    session.append_many([ChatMessage.assistant(content="new", model="test"), summary("successor")])
    assert read(session).page.record_run_ids == ("missing-summary", "successor", "successor")


def test_historical_summary_segments_and_fork_keep_read_identity(manager):
    session = manager.create("coder")
    for run_id in ("one", "two"):
        session = session.start_run(run_id)
        session.append_many(
            [
                ChatMessage.user(run_id),
                ChatMessage.assistant(content=run_id, model="test"),
                summary(run_id),
            ]
        )
    page = read(session)
    assert page.page.record_run_ids == ("one", "one", "one", "two", "two", "two")
    fork = asyncio.run(manager.fork(session.address))
    assert read(fork).page.record_run_ids == page.page.record_run_ids
    assert read(fork).generation_id != page.generation_id


def test_completed_run_does_not_claim_unrelated_later_records(manager):
    session = manager.create("coder")
    session = start(manager, session, "one")
    session.append_many([ChatMessage.assistant(content="one", model="test"), summary("one")])
    manager.get(session.address).append(ChatMessage.user("external"))
    assert read(session, limit=1).page.record_run_ids == (None,)


def test_incremental_reads_are_bounded_and_advance_across_hidden_records(manager):
    session = manager.create("coder")
    baseline = read(session)
    session.append_many(
        [ChatMessage.note("hidden"), ChatMessage.note("also hidden"), ChatMessage.user("visible")]
    )
    first = read(session, after=baseline.after_cursor, limit=2)
    assert first.incremental and first.has_newer
    assert first.page.messages == ()
    second = read(session, after=first.after_cursor, limit=2)
    assert second.incremental and not second.has_newer
    assert second.page.record_sequences == (2,)
    unchanged = read(session, after=second.after_cursor)
    assert unchanged.page.messages == ()
    assert unchanged.after_cursor == second.after_cursor


def test_edit_invalidates_append_cursor_and_excludes_old_lineage(manager):
    session = manager.create("coder")
    user = ChatMessage.user("old")
    session.append_many([user, ChatMessage.assistant(content="old answer", model="test")])
    baseline = read(session)
    replacement = ChatMessage.user("replacement")
    session.append_many([ChatMessage.history_edit(user.id), replacement])
    refreshed = read(session, after=baseline.after_cursor)
    assert not refreshed.incremental
    assert refreshed.page.messages == (replacement,)
    assert refreshed.page.record_sequences == (3,)


def test_recreated_address_invalidates_append_cursor(manager):
    session = manager.create("coder", session_id="same")
    session.append(ChatMessage.user("old"))
    baseline = read(session)
    asyncio.run(manager.archive(session.address))
    replacement = manager.create("coder", session_id="same")
    replacement.append(ChatMessage.user("new"))
    refreshed = read(replacement, after=baseline.after_cursor)
    assert not refreshed.incremental
    assert refreshed.generation_id != baseline.generation_id
    assert refreshed.page.messages[0].content == "new"


def test_duplicate_checkpoint_ids_have_distinct_record_sequences(manager):
    session = manager.create("coder")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="saved", projection=[], compacted_token_count=1
    )
    session.append_many([checkpoint, checkpoint])
    newest = read(session, limit=1)
    older = read(session, before=newest.page.before_cursor, limit=1)
    assert newest.page.messages[0].id == older.page.messages[0].id
    assert newest.page.record_sequences == (1,)
    assert older.page.record_sequences == (0,)


def test_append_cursor_rejects_legacy_ids_and_cannot_combine_with_before(manager):
    session = manager.create("coder")
    cursor = read(session).after_cursor
    with pytest.raises(ChatSessionError, match="before and after"):
        read(session, before=cursor, after=cursor)
    with pytest.raises(ChatSessionError, match="after must be a history cursor"):
        read(session, after="public-message-id")


def test_takeover_replaces_snapshot_to_refresh_prior_user_editability(manager):
    session = manager.create("coder")
    user = ChatMessage.user("prior owner")
    session.append(user)
    baseline = read(session)
    assert user.id in baseline.page.editable_message_ids
    session.append(ChatMessage.agent_takeover(from_address="coder", to_address="reviewer"))
    refreshed = read(session, after=baseline.after_cursor)
    assert not refreshed.incremental
    assert user.id not in refreshed.page.editable_message_ids
    assert [message.role for message in refreshed.page.messages] == ["user", "agent_takeover"]
