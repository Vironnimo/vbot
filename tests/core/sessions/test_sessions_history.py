"""Session history writes: appends, cursors, journals, stored Message shapes."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
from dataclasses import replace

import pytest

import core.sessions._store_codec as session_store_module
from core.chat import ChatMessage
from core.chat.content_blocks import FileMentionBlock, TextBlock
from core.chat.errors import ChatSessionError
from core.chat.messages import MessageSender, ToolCall, ToolCallRejection
from core.chat.output_files import AssistantFileReference
from core.prompts.pinned_context import PINNED_SKILL_CATALOG_SLOT
from core.sessions import (
    ChatSession,
    PromptEpoch,
    SeenSkillsUpdate,
    ToolResultFacts,
    editable_session_message_index,
)
from core.sessions.errors import SessionNotFoundError
from core.sessions.history import skill_tool_activation
from tests.core.sessions.history_fixtures import complete_run
from tests.core.sessions.sessions_test_support import _address, _continuation_start


def test_cursor_reads_only_messages_appended_after_the_snapshot(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.append(ChatMessage.user("first"))
    initial = session.load_since()
    assert initial is not None
    assert len(initial.messages) == 1

    unchanged = session.load_since(initial.cursor)
    assert unchanged is not None
    assert unchanged.messages == () and unchanged.cursor == initial.cursor
    session.append(ChatMessage.assistant(model="test", content="second"))
    appended = session.load_since(initial.cursor)
    assert appended is not None
    assert [message.content for message in appended.messages] == ["second"]
    # A cursor whose anchor names another record belongs to a different history.
    assert session.load_since(replace(initial.cursor, last_message_id="other")) is None
    assert session.load_since(replace(appended.cursor, last_message_id="other")) is None
    assert session.load_since(replace(appended.cursor, next_seq=3)) is None


def test_append_returns_every_record_since_the_cursor_and_commits_its_journal(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.start_run("run-one")
    session.append(ChatMessage.user("first"))
    initial = session.load_since()
    assert initial is not None
    manager.get(session.address).append(ChatMessage.note("written by another accessor"))

    delta = session.append_many(
        [ChatMessage.assistant(model="test", content="second")],
        continuation_records=[_continuation_start()],
        since=initial.cursor,
    )

    assert delta is not None
    assert [message.role for message in delta.messages] == ["note", "assistant"]
    assert [message.role for message in delta.active_messages] == ["note", "assistant"]
    latest = session.load_since()
    assert latest is not None and delta.cursor == latest.cursor
    assert session.load_continuation() is not None


def test_a_rejected_continuation_record_rolls_back_the_whole_append(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.start_run("run-one")
    session.append_many(
        [ChatMessage.user("first")],
        seen_skills=SeenSkillsUpdate(baseline=("alpha",)),
        continuation_records=[_continuation_start()],
    )
    unsupported_version = {**_continuation_start(), "version": 2}
    # Continuation steps start at one.
    step_zero = {
        "version": 1,
        "type": "stream_delta",
        "run_id": "run-one",
        "timestamp": "2026-08-31T12:00:01+00:00",
        "step": 0,
        "content_delta": "lost",
    }

    for rejected in (unsupported_version, step_zero):
        with pytest.raises(ChatSessionError):
            session.append_many(
                [ChatMessage.user("lost")],
                seen_skills=SeenSkillsUpdate(baseline=(), added=("beta",)),
                continuation_records=[rejected],
            )

    assert [message.content for message in session.load()] == ["first"]
    assert manager.seen_skills(session.address) == frozenset({"alpha"})
    state = session.load_continuation()
    assert state is not None
    assert state.steps == ()


def test_compaction_checkpoint_commits_only_while_its_cursor_is_current(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.append(ChatMessage.user("first"))
    snapshot = session.load_since()
    assert snapshot is not None
    affinity = manager.prompt_cache_affinity_id(session.address)
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="Summary.", projection=[], compacted_token_count=1
    )
    manager.get(session.address).append(ChatMessage.note("written by another accessor"))

    epoch = PromptEpoch(
        pins={PINNED_SKILL_CATALOG_SLOT: {"catalog": "new"}}, seen_skills=("alpha",)
    )
    stale = session.commit_compaction(checkpoint, since=snapshot.cursor, epoch=epoch)

    assert stale is None
    assert [message.role for message in session.load()] == ["user", "note"]
    assert manager.prompt_pin(session.address, PINNED_SKILL_CATALOG_SLOT) is None
    assert manager.seen_skills(session.address) is None
    assert manager.prompt_cache_affinity_id(session.address) == affinity

    current = session.load_since()
    assert current is not None
    committed = session.commit_compaction(checkpoint, since=current.cursor, epoch=epoch)

    assert committed is not None
    delta, rotated = committed
    assert [message.id for message in delta.messages] == [checkpoint.id]
    latest = session.load_since()
    assert latest is not None and delta.cursor == latest.cursor
    assert manager.prompt_pin(session.address, PINNED_SKILL_CATALOG_SLOT) == {"catalog": "new"}
    assert manager.seen_skills(session.address) == frozenset({"alpha"})
    assert manager.prompt_cache_affinity_id(session.address) == rotated != affinity


def test_continuation_events_update_one_normalized_current_state(manager) -> None:
    session = manager.create("coder", session_id="continuation-state")
    session.start_run("run-one")
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
                "type": "assistant_boundary",
                "run_id": "run-one",
                "timestamp": "2026-08-31T12:00:03+00:00",
                "step": 1,
                "message_id": "assistant-one",
                "tool_calls": [{"id": "call-one", "name": "bash"}],
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

    state = session.load_continuation()
    assert state is not None
    assert [(step.reasoning, step.content) for step in state.steps] == [
        ("first second", "partial answer")
    ]
    assert [(op.tool_call_id, op.completed, op.ok) for op in state.operations] == [
        ("call-one", True, True)
    ]
    assert (state.active, state.cause) == (False, "user")
    with sqlite3.connect(manager._store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "continuation_records" not in tables
        assert connection.execute("SELECT COUNT(*) FROM continuations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM continuation_steps").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM continuation_operations").fetchone()[0] == 1


def test_skill_activation_cache_follows_checkpoints_and_history_edits(manager, monkeypatch) -> None:
    session = manager.create("coder", session_id="skill-cache")
    session.append(ChatMessage.user("start"))
    session.activate_skill_context("alpha", {"activation_content": "ALPHA"})
    skill_result = ChatMessage.tool(
        tool_call_id="skill-1",
        name="skill",
        content=json.dumps(
            {"ok": True, "data": {"status": "loaded", "name": "gamma", "content": "GAMMA"}}
        ),
    )
    session.start_run("run-1").append_many(
        [
            ChatMessage.assistant(
                model="test",
                content=None,
                tool_calls=[ToolCall(id="skill-1", name="skill", arguments={"name": "gamma"})],
            ),
            skill_result,
        ]
    )
    gamma = skill_tool_activation(skill_result)
    assert gamma is not None
    edited = ChatMessage.user("edited away")
    session.append(edited)
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="checkpoint", projection=[], compacted_token_count=1
    )

    def no_full_load(_self):
        raise AssertionError("the Skill cache must not load the full history")

    monkeypatch.setattr(ChatSession, "load", no_full_load)
    session.append_many([checkpoint])
    assert session.activated_skill_contents() == {}
    session.activate_skill_context("beta", {"activation_content": "BETA"})
    assert session.activated_skill_contents() == {"beta": "BETA"}
    assert manager.get(session.address).activated_skill_contents() == {"beta": "BETA"}

    # The edit deactivates the checkpoint and the later activation; the
    # activations before the edited message become current again.
    session.apply_edit(edited.id, [ChatMessage.user("replacement")])

    expected = {"alpha": "ALPHA", gamma[0]: gamma[1]}
    assert session.activated_skill_contents() == expected
    assert manager.get(session.address).activated_skill_contents() == expected


def test_edit_targets_only_an_own_plain_text_user_message_after_the_latest_takeover() -> None:
    first = ChatMessage.user("first")
    replacement = ChatMessage.user("replacement")

    assert editable_session_message_index([first, replacement], replacement.id) == 1
    with pytest.raises(ChatSessionError, match="non-empty"):
        editable_session_message_index([first], "")
    with pytest.raises(ChatSessionError, match="not active"):
        editable_session_message_index([replacement], first.id)

    structured = ChatMessage.user([])
    with pytest.raises(ChatSessionError, match="plain-text"):
        editable_session_message_index([structured], structured.id)

    takeover = ChatMessage.agent_takeover(from_address="alpha", to_address="beta")
    with pytest.raises(ChatSessionError, match="takeover"):
        editable_session_message_index([first, takeover], first.id)


def test_deferred_notes_keep_their_existing_ordering(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.begin_defer_notes()
    session.add_note("first")
    session.add_note("second")

    session.flush_deferred_notes()

    assert [message.content for message in session.load()] == ["first", "second"]
    assert [message.content for message in session.drain_pending_notes()] == ["first", "second"]


def test_tool_result_persisted_needs_the_call_and_its_result_in_that_session(manager) -> None:
    session = manager.create("coder", session_id="handoff")
    other = manager.create("coder", session_id="other")
    run = session.start_run("run-one")
    assistant = ChatMessage.assistant(
        model="test",
        content=None,
        tool_calls=[ToolCall(id="call-one", name="bash", arguments={"command": "vbot update"})],
    )
    run.append_many([ChatMessage.user("update"), assistant])
    run.assistant_message_id = assistant.id

    def persisted(address) -> bool:
        return bool(asyncio.run(manager.tool_result_persisted_async(address, "call-one")))

    # Requested but not yet answered is not durable.
    assert persisted(session.address) is False
    run.append(ChatMessage.tool(tool_call_id="call-one", name="bash", content="ok"))
    assert persisted(session.address) is True
    assert persisted(other.address) is False
    with pytest.raises(SessionNotFoundError):
        persisted(_address("coder", "missing"))


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
        reasoning_summary=["**First**\n\nReasoning", "**Second**\n\nMore reasoning"],
        reasoning_meta={"provider_state": {"opaque": True}},
        reasoning_scope="turn",
        reasoning_timing={
            "started_at": "2026-08-31T12:00:00.000000Z",
            "completed_at": "2026-08-31T12:00:01.000000Z",
            "duration_ms": 1000,
            "clock": "monotonic",
        },
        phase="analysis",
        usage={
            "input_tokens": 12,
            "output_tokens": 4,
            "cache_read_tokens": 2,
            "output_tokens_estimated": True,
            "estimated": True,
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
            "started_at": "2026-08-31T12:00:01.000000Z",
            "completed_at": "2026-08-31T12:00:02.000000Z",
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
            "started_at": "2026-08-31T12:00:00.000000Z",
            "completed_at": "2026-08-31T12:00:03.000000Z",
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

    assert run_summary.timing is not None
    run_summary = dataclasses.replace(run_summary, timestamp=run_summary.timing["completed_at"])
    facts = ToolResultFacts(
        "failed", ok=False, error_code="denied", error_retryable=False, error_attempts=2
    )
    writer = session.start_run("run-one")
    writer.append_many([user, assistant, tool], tool_results={"call-one": facts})
    stored_summary = complete_run(writer, run_summary)

    assert stored_summary == dataclasses.replace(run_summary, id=stored_summary.id)
    assert session.load() == [
        *(dataclasses.replace(message, run_id="run-one") for message in [user, assistant, tool]),
        stored_summary,
    ]
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        # The Tool result's text lives once, in its own entry; the call keeps
        # the outcome Chat reported.
        assert connection.execute(
            """
            SELECT c.status, c.result_ok, c.error_code, c.error_retryable, c.error_attempts,
                   t.content
            FROM tool_calls AS c
            JOIN entries AS e ON e.entry_key = c.result_entry_key
            JOIN entry_text AS t ON t.entry_key = e.entry_key
            WHERE c.call_id = 'call-one'
            """
        ).fetchone() == ("failed", 0, "denied", 0, 2, tool.content)
        # A call left without a result ends with its interrupted Run.
        assert connection.execute(
            "SELECT status, result_entry_key FROM tool_calls WHERE call_id = 'call-two'"
        ).fetchone() == ("interrupted", None)
        # Usage provenance is stored per counter; the whole-turn summary is derived on read.
        assert connection.execute(
            "SELECT input_tokens_estimated, output_tokens_estimated, usage_extra_json "
            "FROM assistant_entries WHERE usage_present = 1"
        ).fetchone() == (None, 1, '{"provider_detail":{"tier":"test"}}')
        entries_before = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def fail_message_reconstruction(*_args, **_kwargs):
        raise AssertionError("A fork must not read or copy Messages")

    with monkeypatch.context() as patch:
        patch.setattr(session_store_module, "insert_entry", fail_message_reconstruction)
        forked = asyncio.run(manager.fork(session.address, target_agent_id="reviewer"))

    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == entries_before
    assert forked.load_active() == session.load()
    fork_hits = manager.search_messages(
        "normalized",
        project_id=forked.address.project_id,
        agent_id=forked.address.agent_id,
        session_id=forked.address.session_id,
    ).hits
    assert [(hit.message_id, hit.address) for hit in fork_hits] == [(user.id, forked.address)]
