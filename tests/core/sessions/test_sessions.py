"""Tests for sessions."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3

import pytest

import core.sessions._store_codec as session_store_module
from core.chat import ChatMessage
from core.chat.content_blocks import FileMentionBlock, TextBlock
from core.chat.errors import ChatSessionError
from core.chat.messages import MessageSender, ToolCall, ToolCallRejection
from core.chat.output_files import AssistantFileReference
from core.sessions import (
    FORK_SOURCE_META_KEY,
    ChatSession,
    ToolResultFacts,
)
from core.sessions import _store_values as store_values
from core.sessions.errors import SessionNotFoundError
from core.sessions.history import skill_tool_activation
from tests.core.sessions.history_fixtures import complete_run
from tests.core.sessions.sessions_test_support import (
    _address,
    _continuation_start,
)
from tests.core.sessions.sessions_test_support import (
    manager as manager,
)


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


def test_continuation_steps_start_at_one_and_a_rejected_record_rolls_back_its_append(
    manager,
) -> None:
    session = manager.create("coder", session_id="continuation-steps")
    session.start_run("run-one")
    session.append_continuation_records([_continuation_start()])
    step_zero = {
        "version": 1,
        "type": "stream_delta",
        "run_id": "run-one",
        "timestamp": "2026-08-31T12:00:01+00:00",
        "step": 0,
        "content_delta": "lost",
    }

    with pytest.raises(ChatSessionError):
        session.append_many([ChatMessage.user("not committed")], continuation_records=[step_zero])

    assert session.load() == []
    state = session.load_continuation()
    assert state is not None
    assert state.steps == ()


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


def test_fork_inherits_history_but_not_activity_or_continuation(manager) -> None:
    source = manager.create("coder", session_id="source")
    source.append_many(
        [ChatMessage.user("hello"), ChatMessage.assistant(model="test", content="hi")]
    )
    source.start_run("run-one")
    source.append_continuation_record(_continuation_start())
    source_address = _address("coder", "source")
    manager.record_terminal_run(source_address, "run-1", "completed", "2026-08-29T12:00:00Z")

    forked = asyncio.run(manager.fork(source_address, target_agent_id="reviewer"))

    # The fork's current view shows the inherited history; its own audit is empty.
    assert forked.load_active() == source.load_active()
    assert forked.load() == []
    assert forked.load_continuation() is None
    assert manager.list_completion_activity([(None, "reviewer")]) == {(None, "reviewer"): []}
    metadata = manager.get_metadata(forked.address)
    assert metadata[FORK_SOURCE_META_KEY]["session_id"] == "source"
    # A fork into another Agent's scope starts its own prompt-cache lineage.
    assert manager.prompt_cache_affinity_id(forked.address) != manager.prompt_cache_affinity_id(
        source_address
    )


def test_metadata_value_reads_one_projected_or_residual_value(manager, monkeypatch) -> None:
    address = _address("coder", "narrow")
    manager.create(address.agent_id, session_id=address.session_id)
    policy = {"enabled": False}
    manager.set_metadata(address, {"compaction_policy": policy, "title": "Named", "flag": None})

    def no_metadata_decode(_address):
        raise AssertionError("a single value must not decode the complete metadata")

    with monkeypatch.context() as patch:
        patch.setattr(manager._store, "metadata", no_metadata_decode)
        assert manager.metadata_value(address, "compaction_policy") == policy
        assert manager.metadata_value(address, "title") == "Named"
        assert manager.metadata_value(address, "flag") is None
        assert manager.metadata_value(address, "missing") is None
    # A projected value must fit its column; a failed write changes nothing.
    with pytest.raises(ChatSessionError, match="compaction_policy must be an object"):
        manager.set_metadata(address, {"compaction_policy": "not-an-object"})
    assert manager.metadata_value(address, "compaction_policy") == policy
    manager.set_metadata(address, {"flag": True})
    assert manager.metadata_value(address, "flag") is True
    assert manager.metadata_value(address, "title") is None
    with pytest.raises(SessionNotFoundError):
        manager.metadata_value(_address("coder", "missing"), "title")


def test_prompt_cache_affinity_id_is_prompt_state_not_metadata(manager, monkeypatch) -> None:
    address = _address("coder", "affinity")
    session = manager.create(address.agent_id, session_id=address.session_id)
    default = manager.prompt_cache_affinity_id(address)
    assert manager.prompt_cache_affinity_id(address) == default
    user = ChatMessage.user("first")
    session.append(user)
    edited = session.apply_edit(user.id, [ChatMessage.user("second")])

    def no_metadata_decode(_address):
        raise AssertionError("the affinity id must not decode the complete metadata")

    with monkeypatch.context() as patch:
        patch.setattr(manager._store, "metadata", no_metadata_decode)
        assert manager.prompt_cache_affinity_id(address) == edited.prompt_cache_affinity_id
    assert edited.prompt_cache_affinity_id != default
    assert "prompt_cache_affinity_id" not in manager.get_metadata(address)
    with pytest.raises(ChatSessionError, match="dedicated APIs"):
        manager.set_metadata(address, {"prompt_cache_affinity_id": "chosen"})
    with pytest.raises(ChatSessionError, match="dedicated APIs"):
        manager.set_metadata(address, {"pinned_skill_catalog": {"catalog_text": "x"}})
    with pytest.raises(SessionNotFoundError):
        manager.prompt_cache_affinity_id(_address("coder", "missing"))


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


def _count_writes(manager, monkeypatch) -> list[object]:
    database = manager._store.database
    original = database.write
    writes: list[object] = []

    def counted(fn, **kwargs):
        writes.append(fn)
        return original(fn, **kwargs)

    monkeypatch.setattr(database, "write", counted)
    return writes


def _state_revision(manager, address) -> int:
    return int(
        manager._store._read(
            lambda connection: connection.execute(
                "SELECT state_revision FROM sessions WHERE project_id = ? AND agent_id = ? "
                "AND session_id = ? AND state = 'live'",
                (address.project_id or "", address.agent_id, address.session_id),
            ).fetchone()[0]
        )
    )


def test_get_or_create_reads_an_existing_session_without_a_write(manager, monkeypatch) -> None:
    address = _address("coder", "existing")
    writes = _count_writes(manager, monkeypatch)

    created = manager.get_or_create(address)
    assert len(writes) == 1
    assert manager.get_or_create(address).address == created.address
    assert len(writes) == 1


def test_ensure_metadata_writes_only_a_real_change(manager, monkeypatch) -> None:
    address = _address("coder", "channel")
    writes = _count_writes(manager, monkeypatch)

    def route(metadata):
        metadata["platform"] = "telegram"

    with pytest.raises(SessionNotFoundError):
        manager.ensure_metadata(address, route)
    assert writes == []

    previous, updated = manager.ensure_metadata(address, route, create_missing=True)
    # A missing Session and its metadata commit together.
    assert len(writes) == 1
    assert previous == {}
    assert updated == {"platform": "telegram"}
    assert manager.get_metadata(address)["platform"] == "telegram"

    revision = _state_revision(manager, address)
    previous, updated = manager.ensure_metadata(address, route, create_missing=True)
    assert len(writes) == 1
    assert previous == updated == manager.get_metadata(address)
    assert _state_revision(manager, address) == revision

    manager.set_title(address, "Concurrent title")
    writes.clear()
    previous, updated = manager.ensure_metadata(
        address, lambda metadata: metadata.__setitem__("platform", "discord")
    )
    # The writer reapplies the mutation to the latest row, keeping other edits.
    assert len(writes) == 1
    assert previous["platform"] == "telegram"
    assert manager.get_metadata(address)["platform"] == "discord"
    assert manager.get_metadata(address)["title"] == "Concurrent title"


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


def test_session_list_order_queries_use_declared_indexes(manager, tmp_path) -> None:
    manager.create("coder", session_id="one", project_id=None)
    manager.create("reviewer", session_id="two", project_id="project")

    def plan(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> str:
        return " ".join(
            str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, params)
        )

    scopes = ("", "coder", "project", "reviewer", store_values._LIST_VISIBILITY_BACKGROUND)
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        scoped_plan = plan(
            connection,
            "SELECT session_id FROM sessions WHERE state = 'live' AND project_id = ? "
            "AND agent_id = ? ORDER BY last_activity_at DESC, session_id LIMIT 20",
            ("", "coder"),
        )
        global_plan = plan(
            connection,
            "WITH candidates AS (SELECT session_id, last_activity_at, project_id, agent_id "
            "FROM sessions WHERE state = 'live' AND ((project_id = ? AND agent_id = ?) "
            "OR (project_id = ? AND agent_id = ?)) AND (list_visibility_mask & ?) = 0) "
            "SELECT session_id FROM candidates ORDER BY last_activity_at DESC, project_id, "
            "agent_id, session_id LIMIT 20",
            scopes,
        )
        count_plan = plan(
            connection,
            "SELECT COUNT(*) FROM sessions WHERE state = 'live' AND ((project_id = ? AND "
            "agent_id = ?) OR (project_id = ? AND agent_id = ?)) "
            "AND (list_visibility_mask & ?) = 0",
            scopes,
        )

    assert "sessions_live_scope_order" in scoped_plan
    assert "USE TEMP B-TREE" not in scoped_plan
    assert "sessions_live_global_order" in global_plan
    assert "USE TEMP B-TREE" not in global_plan
    assert "COVERING INDEX sessions_live_scope_visibility" in count_plan
