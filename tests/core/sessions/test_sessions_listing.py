"""Tests for sessions listing."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from typing import Any

import pytest

import core.sessions._store_codec as session_store_module
from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.prompts.pinned_context import PINNED_MEMORY_FILES_SLOT, PINNED_SKILL_CATALOG_SLOT
from core.runs import Run, RunKind
from core.sessions import (
    SESSION_RUN_KINDS_META_KEY,
    PromptEpoch,
    SeenSkillsUpdate,
    SessionAddress,
    SessionListFilters,
)
from core.utils.timestamps import canonical_timestamp
from tests.core.sessions.history_fixtures import admit_run, complete_run, history_revision
from tests.core.sessions.sessions_test_support import (
    _address,
    _continuation_start,
)
from tests.core.sessions.sessions_test_support import (
    manager as manager,
)


def _classify(manager, address: SessionAddress, metadata: Any) -> None:
    """Write list metadata and Run kinds the way their owners do.

    A Run kind this version does not know is written directly, as a newer vBot
    sharing the database would.
    """
    metadata = dict(metadata)
    run_kinds = metadata.pop("run_kinds", [])
    if metadata:
        manager.set_metadata(address, metadata)
    known = {kind.value for kind in RunKind}
    for run_kind in run_kinds:
        if run_kind in known:
            asyncio.run(admit_run(manager, address, RunKind(run_kind)))
            continue
        with sqlite3.connect(manager._store.path) as connection:
            connection.execute(
                "INSERT INTO session_run_kinds (session_key, run_kind) "
                "SELECT session_key, ? FROM sessions WHERE project_id = ? AND agent_id = ? "
                "AND session_id = ? AND state = 'live'",
                (run_kind, address.project_id or "", address.agent_id, address.session_id),
            )


def _state_revision(manager, address: SessionAddress) -> int:
    return int(
        manager._store._read(
            lambda connection: connection.execute(
                "SELECT state_revision FROM sessions WHERE project_id = ? AND agent_id = ? "
                "AND session_id = ? AND state = 'live'",
                (address.project_id or "", address.agent_id, address.session_id),
            ).fetchone()[0]
        )
    )


def test_create_append_and_load_use_a_canonical_database(manager, tmp_path) -> None:
    session = manager.create("coder", session_id="session-one")
    messages = [ChatMessage.user("hello"), ChatMessage.assistant(model="test", content="hi")]

    session.append_many(messages)

    assert session.load() == messages
    assert session.load_active() == messages
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


def test_an_append_commits_its_seen_skills_in_the_same_transaction(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.start_run("run-one")

    session.append_many(
        [ChatMessage.user("first")], seen_skills=SeenSkillsUpdate(baseline=("alpha",))
    )
    with pytest.raises(ChatSessionError):
        session.append_many(
            [ChatMessage.user("lost")],
            seen_skills=SeenSkillsUpdate(baseline=(), added=("beta",)),
            continuation_records=[{**_continuation_start(), "version": 2}],
        )

    assert [message.content for message in session.load()] == ["first"]
    assert manager.seen_skills(session.address) == frozenset({"alpha"})


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

    async def run(session, run_id, run_kind, result):
        admitted = Run(
            run_id=run_id,
            agent_id=session.address.agent_id,
            session_id=session.id,
            project_id=session.address.project_id,
            run_kind=run_kind,
        )
        await manager.start_run(admitted)
        complete_run(session.for_run(run_id), summary(run_id, result))
        return admitted

    await run(source, "inherited", RunKind.USER, "completed")
    fork = await manager.fork(
        source.address, target_project_id=project_id, run_kind=RunKind.MEMORY_REFLECTION
    )
    # A classified fork with only inherited summaries must not fabricate a result.
    assert source.reflection_runs() == []
    review = await run(fork, "review", RunKind.MEMORY_REFLECTION, status)
    # Later user work inside the review Session must not replace its review result.
    await run(fork, "later-user", RunKind.USER, "completed")
    other = manager.create("coder", session_id="other", project_id=project_id)
    other_fork = await manager.fork(other.address, target_project_id=project_id)
    await run(other_fork, "other-review", RunKind.SKILL_REFLECTION, "completed")
    # A fork into another Agent does not review this Session for its Agent.
    elsewhere = await manager.fork(
        source.address, target_agent_id="reviewer", target_project_id=project_id
    )
    await run(elsewhere, "elsewhere-review", RunKind.MEMORY_REFLECTION, "completed")
    other_scope = manager.create("coder", session_id="source", project_id="different-project")

    def forbid_history(*args, **kwargs):
        raise AssertionError("Reflection recovery must not reconstruct chat content")

    monkeypatch.setattr(session_store_module, "select_batch", forbid_history)
    assert source.reflection_runs() == [
        {
            "session_id": fork.id,
            "run_id": "review",
            "status": status,
            "started_at": canonical_timestamp(review.created_at),
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
    session.start_run("run-one")
    revision = history_revision(manager, address)

    manager.set_metadata(address, {"project": "vbot"})
    manager.record_terminal_run(address, "run-1", "completed", "2026-08-29T12:00:00Z")
    session.append_continuation_records([_continuation_start()])

    assert history_revision(manager, address) == revision
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
    manager.set_metadata(address, {"title": "Kept"})
    manager.record_seen_skills(address, SeenSkillsUpdate(baseline=("alpha",)))
    asyncio.run(admit_run(manager, address))
    manager.record_terminal_run(address, "run-1", "completed", "2026-08-29T12:00:00Z")
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is True
    writer = manager._store._writer
    revision = _state_revision(manager, address)
    changes = writer.total_changes

    previous, updated = manager.mutate_metadata_with_previous(
        address, lambda metadata: metadata.update(title="Kept")
    )
    manager.record_seen_skills(address, SeenSkillsUpdate(baseline=(), added=("alpha",)))
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is False

    assert previous == updated
    assert writer.total_changes == changes
    assert _state_revision(manager, address) == revision

    asyncio.run(admit_run(manager, address, RunKind.CRON))
    assert _state_revision(manager, address) > revision
    # Run kinds form a set, reported in name order.
    assert manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY] == [
        RunKind.CRON.value,
        RunKind.USER.value,
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
        "subagent_parent": {
            "id": "work",
            "agent_id": "parent",
            "session_id": "root",
            "run_id": "parent-run",
            "tool_call_id": "call",
            "tool_call_index": 1,
            "project_id": None,
        },
        "compaction_policy": {"enabled": False},
        "extension_state": "x" * 100_000,
    }

    manager.set_metadata(address, metadata)
    asyncio.run(admit_run(manager, address, RunKind.SUBAGENT))

    assert manager.get_metadata(address) == {**metadata, "run_kinds": ["subagent"]}
    with sqlite3.connect(manager._store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT session_key, metadata_json, title, subagent_parent_session_id, "
            "subagent_parent_tool_call_index, compaction_policy_json FROM sessions "
            "WHERE agent_id = ? AND session_id = ?",
            (address.agent_id, address.session_id),
        ).fetchone()
        assert row is not None
        run_kinds = connection.execute(
            "SELECT run_kind FROM session_run_kinds WHERE session_key = ?",
            (row["session_key"],),
        ).fetchall()
    assert json.loads(row["metadata_json"]) == {"extension_state": "x" * 100_000}
    assert row["title"] == "Release planning"
    assert row["subagent_parent_session_id"] == "root"
    assert row["subagent_parent_tool_call_index"] == 1
    assert json.loads(row["compaction_policy_json"]) == {"enabled": False}
    assert [tuple(kind) for kind in run_kinds] == [("subagent",)]


@pytest.mark.timeout(120)
def test_session_list_page_is_bounded_filtered_and_keeps_required_session(manager) -> None:
    # The paging fixture writes forty durable Sessions with large metadata payloads.
    normal_ids: list[str] = []
    for index in range(40):
        session_id = f"normal-{index:02d}"
        address = _address("coder", session_id)
        manager._store.create(address, created_at=f"2026-08-01T12:{index:02d}:00+00:00")
        _classify(manager, address, {"title": f"Normal {index}", "run_kinds": ["user"]})
        manager.ensure_prompt_pin(
            address, PINNED_SKILL_CATALOG_SLOT, {"catalog": "large" * 10_000}, lambda _pin: True
        )
        normal_ids.append(session_id)
    hidden = _address("coder", "cron-hidden")
    manager._store.create(hidden, created_at="2026-08-01T00:00:00+00:00")
    _classify(manager, hidden, {"run_kinds": ["cron"]})

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
    assert all(PINNED_SKILL_CATALOG_SLOT not in summary for summary in first.sessions)
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
    manager.ensure_prompt_pin(
        unread, PINNED_MEMORY_FILES_SLOT, {"files": "large" * 10_000}, lambda _pin: True
    )
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
                "unread_run_at": "2026-08-29T12:00:00.000000Z",
            },
        ],
        ("vbot", "coder"): [
            {
                "id": "team",
                "latest_completion_run_id": "run-3",
                "has_unread_completion": True,
                "unread_run_id": "run-3",
                "unread_run_status": "completed",
                "unread_run_at": "2026-08-29T12:02:00.000000Z",
            }
        ],
        (None, "unknown"): [],
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
    read = manager._store.database.read

    def counting_read_ctx(*args, **kwargs):
        nonlocal snapshots
        snapshots += 1
        return read(*args, **kwargs)

    monkeypatch.setattr(manager._store.database, "read", counting_read_ctx)

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


def test_newest_session_counts_every_run_kind_but_no_extension_session(manager) -> None:
    assert manager.newest_session_id("coder") is None
    for index, session_id in enumerate(("older", "reflection")):
        address = _address("coder", session_id)
        manager._store.create(address, created_at=f"2026-08-01T00:0{index}:00+00:00")
    asyncio.run(admit_run(manager, _address("coder", "reflection"), RunKind.MEMORY_REFLECTION))
    manager.create_bound_temporary_session(
        _address("coder", "participant"),
        owner_name="swarm",
        group_id="swr_group",
        participant_id="prt_peer",
        config={},
    )

    # The Extension-owned Session is the newest row, yet only listable ones count.
    assert manager.newest_session_id("coder") == "reflection"
    assert manager.newest_session_id("other") is None


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
        _classify(manager, address, metadata)

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


RECALL_VISIBILITY_CASES = {
    "legacy": ({}, "conversation"),
    "unknown-kind": ({"run_kinds": ["future_kind"]}, "conversation"),
    "user": ({"run_kinds": ["user"]}, "conversation"),
    "channel": ({"run_kinds": ["channel"]}, "conversation"),
    "cron": ({"run_kinds": ["cron"]}, "conversation"),
    "calendar": ({"run_kinds": ["calendar"]}, "conversation"),
    "user-system": ({"run_kinds": ["user", "system"]}, "conversation"),
    "system": ({"run_kinds": ["system"]}, "hidden"),
    "reflection": ({"run_kinds": ["reflection"]}, "hidden"),
    "memory": ({"run_kinds": ["memory_reflection"]}, "hidden"),
    "user-skill": ({"run_kinds": ["user", "skill_reflection"]}, "hidden"),
    "subagent-kind": ({"run_kinds": ["subagent"]}, "subagent"),
    "subagent-flag": ({"is_subagent_session": True}, "subagent"),
    "subagent-user": ({"is_subagent_session": True, "run_kinds": ["user"]}, "subagent"),
    "subagent-reflection": (
        {"is_subagent_session": True, "run_kinds": ["reflection"]},
        "hidden",
    ),
    "not-subagent-system": ({"is_subagent_session": False, "run_kinds": ["system"]}, "hidden"),
}


def test_recall_visibility_is_classified_in_sql(manager) -> None:
    for session_id, (metadata, _expected) in RECALL_VISIBILITY_CASES.items():
        manager.create("coder", session_id=session_id)
        _classify(manager, _address("coder", session_id), metadata)
    expected = {
        session_id: visibility
        for session_id, (_metadata, visibility) in RECALL_VISIBILITY_CASES.items()
    }

    revisions = manager.list_history_revisions("coder")
    sources = manager.descriptor_sources([_address("coder", session_id) for session_id in expected])

    assert {revision.address.session_id: revision.recall_visibility for revision in revisions} == (
        expected
    )
    assert {
        address.session_id: source.recall_visibility for address, source in sources.items()
    } == expected


def test_history_revisions_order_sessions_by_creation(manager) -> None:
    for session_id in ("b-first", "a-second", "c-third"):
        manager.create("coder", session_id=session_id)

    revisions = manager.list_history_revisions("coder")
    order = {revision.address.session_id: revision.creation_order for revision in revisions}

    assert order["b-first"] < order["a-second"] < order["c-third"]


@pytest.mark.parametrize("include_channels", [False, True])
def test_channel_filter_counts_pages_and_preserves_required_session(manager, include_channels):
    for index, session_id in enumerate(["old", "telegram", "new", "discord"]):
        address = _address("coder", session_id)
        manager._store.create(address, created_at=f"2026-09-01T00:0{index}:00+00:00")
        if session_id in {"telegram", "discord"}:
            _classify(
                manager,
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
