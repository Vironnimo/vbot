"""Tests for sessions listing."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

import core.sessions._store_codec as session_store_module
from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.runs import RunKind
from core.sessions import (
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SESSION_RUN_KINDS_META_KEY,
    SessionListFilters,
)
from tests.core.sessions.sessions_test_support import (
    _address,
    _continuation_start,
)
from tests.core.sessions.sessions_test_support import (
    manager as manager,
)


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


def test_a_rejected_journal_record_rolls_back_its_history_append(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.append(ChatMessage.user("first"))

    with pytest.raises(ChatSessionError):
        session.append_many(
            [ChatMessage.assistant(model="test", content="lost")],
            continuation_records=[{**_continuation_start(), "version": 2}],
        )

    assert [message.role for message in session.load()] == ["user"]
    assert session.load_continuation() is None


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

    source.start_run("inherited").append(summary("inherited", "completed"))
    fork = await manager.fork(
        source.address,
        target_project_id=project_id,
        strip_meta_keys=SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    )
    manager.record_run_kind(fork.address, RunKind.MEMORY_REFLECTION)
    # An admitted fork with only copied summaries must not fabricate a result.
    assert source.reflection_runs() == []
    fork.start_run("review").append(summary("review", status))
    # Later user work inside the review Session must not replace its review result.
    manager.record_run_kind(fork.address, RunKind.USER)
    fork.start_run("later-user").append(summary("later-user", "completed"))
    other = manager.create("coder", session_id="other", project_id=project_id)
    other_fork = await manager.fork(
        other.address,
        target_project_id=project_id,
        strip_meta_keys=SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    )
    manager.record_run_kind(other_fork.address, RunKind.SKILL_REFLECTION)
    other_fork.start_run("other-review").append(summary("other-review", "completed"))
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
    continuation = session.load_continuation()
    assert continuation is not None
    assert continuation.checkpoint_id == "checkpoint-one"
    assert manager.mark_terminal_run_read(address, "wrong")["marked_read"] is False
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is True


def test_unchanged_metadata_and_activity_mutations_write_nothing(manager) -> None:
    address = _address("coder", "unchanged-mutations")
    manager.create(address.agent_id, session_id=address.session_id)
    manager.set_metadata(address, {"title": "Kept", "seen_skills": ["alpha"]})
    manager.record_run_kind(address, RunKind.USER)
    manager.record_terminal_run(address, "run-1", "completed", "2026-08-29T12:00:00Z")
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is True
    writer = manager._store._writer
    revision = manager._store.state(address)["state_revision"]
    changes = writer.total_changes

    manager.record_run_kind(address, RunKind.USER)
    previous, updated = manager.mutate_metadata_with_previous(
        address, lambda metadata: metadata.update(title="Kept", seen_skills=["alpha"])
    )
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is False

    assert previous == updated
    assert writer.total_changes == changes
    assert manager._store.state(address)["state_revision"] == revision

    manager.record_run_kind(address, RunKind.CRON)
    assert manager._store.state(address)["state_revision"] == revision + 1
    assert manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY] == [
        RunKind.USER.value,
        RunKind.CRON.value,
    ]


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


@pytest.mark.timeout(120)
def test_session_list_page_is_bounded_filtered_and_keeps_required_session(manager) -> None:
    # The paging fixture writes forty durable Sessions with large metadata payloads.
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


def test_completion_activity_reads_completed_sessions_for_many_scopes(manager) -> None:
    unread = _address("coder", "unread")
    read = _address("coder", "read")
    idle = _address("coder", "idle")
    project = _address("coder", "team", "vbot")
    for address in (unread, read, idle, project):
        manager.create(
            address.agent_id, session_id=address.session_id, project_id=address.project_id
        )
    manager.set_metadata(unread, {"pinned_memory_files": "large" * 10_000})
    manager.record_terminal_run(unread, "run-1", "failed", "2026-08-29T12:00:00Z")
    manager.record_terminal_run(read, "run-2", "completed", "2026-08-29T12:01:00Z")
    manager.mark_terminal_run_read(read, "run-2")
    manager.record_terminal_run(project, "run-3", "completed", "2026-08-29T12:02:00Z")

    activity = manager.list_completion_activity(
        [(None, "coder"), ("vbot", "coder"), (None, "unknown"), (None, "coder")]
    )

    assert activity == {
        (None, "coder"): [
            {
                "id": "read",
                "latest_completion_run_id": "run-2",
                "has_unread_completion": False,
                "unread_run_id": None,
                "unread_run_status": None,
                "unread_run_at": None,
            },
            {
                "id": "unread",
                "latest_completion_run_id": "run-1",
                "has_unread_completion": True,
                "unread_run_id": "run-1",
                "unread_run_status": "failed",
                "unread_run_at": "2026-08-29T12:00:00Z",
            },
        ],
        ("vbot", "coder"): [
            {
                "id": "team",
                "latest_completion_run_id": "run-3",
                "has_unread_completion": True,
                "unread_run_id": "run-3",
                "unread_run_status": "completed",
                "unread_run_at": "2026-08-29T12:02:00Z",
            }
        ],
        (None, "unknown"): [],
    }
    rows = manager._store.list_completion_activity_rows([(None, "coder")])
    assert set(rows[0].keys()) == {
        "project_id",
        "agent_id",
        "session_id",
        "latest_completion_run_id",
        "latest_completion_status",
        "latest_completion_at",
        "read_completion_run_id",
    }


def test_completion_activity_reads_all_scopes_in_one_snapshot(manager, monkeypatch) -> None:
    from core.sessions import _store_queries

    monkeypatch.setattr(_store_queries, "_COMPLETION_ACTIVITY_SCOPE_BATCH_SIZE", 2)
    scopes = [(None, f"agent-{index}") for index in range(5)]
    for _project_id, agent_id in scopes:
        address = _address(agent_id, "done")
        manager.create(agent_id, session_id="done")
        manager.record_terminal_run(address, f"run-{agent_id}", "completed", "2026-08-29T12:00:00Z")
    snapshots = 0
    read_ctx = manager._store._runtime.read_ctx

    def counting_read_ctx(*args, **kwargs):
        nonlocal snapshots
        snapshots += 1
        return read_ctx(*args, **kwargs)

    monkeypatch.setattr(manager._store._runtime, "read_ctx", counting_read_ctx)

    activity = manager.list_completion_activity(scopes)

    assert snapshots == 1
    assert {scope: [row["id"] for row in rows] for scope, rows in activity.items()} == {
        scope: ["done"] for scope in scopes
    }


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


@pytest.mark.parametrize("include_channels", [False, True])
def test_channel_filter_counts_pages_and_preserves_required_session(manager, include_channels):
    for index, session_id in enumerate(["old", "telegram", "new", "discord"]):
        address = _address("coder", session_id)
        manager._store.create(address, created_at=f"2026-09-01T00:0{index}:00+00:00")
        if session_id in {"telegram", "discord"}:
            manager.set_metadata(
                address,
                {"platform": session_id, "platform_conv_id": "chat-1", "run_kinds": ["cron"]},
            )
    filters = SessionListFilters(include_channels=include_channels, include_cron=False)
    first = manager.list_summaries_page([(None, "coder")], limit=1, filters=filters)
    assert first.total_count == (4 if include_channels else 2)
    assert first.sessions[0]["id"] == ("discord" if include_channels else "new")
    assert first.next_cursor is not None
    second = manager.list_summaries_page(
        [(None, "coder")], limit=10, cursor=first.next_cursor, filters=filters
    )
    assert [row["id"] for row in second.sessions] == (
        ["new", "telegram", "old"] if include_channels else ["old"]
    )
    assert second.next_cursor is None
    required = manager.list_summaries_page(
        [(None, "coder")], limit=1, filters=filters, required_address=_address("coder", "telegram")
    )
    assert [row["id"] for row in required.sessions] == (
        ["discord", "telegram"] if include_channels else ["new", "telegram"]
    )
    assert required.total_count == (4 if include_channels else 3)
    assert required.next_cursor == first.next_cursor
