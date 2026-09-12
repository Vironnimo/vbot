"""Tests for sessions listing."""

from __future__ import annotations

import json
import sqlite3

import pytest

import core.sessions._store_codec as session_store_module
from core.chat import ChatMessage
from core.chat.continuation import fold_continuation_records
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
