"""Session lists, completion activity, recall visibility and review summaries."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

import core.sessions._store_codec as session_store_module
from core.chat import ChatMessage
from core.prompts.pinned_context import PINNED_MEMORY_FILES_SLOT, PINNED_SKILL_CATALOG_SLOT
from core.runs import Run, RunKind
from core.sessions import (
    SUBAGENT_PARENT_META_KEY,
    SUBAGENT_SESSION_META_KEY,
    SessionAddress,
    SessionListFilters,
)
from core.utils.timestamps import canonical_timestamp
from tests.core.sessions.history_fixtures import (
    admit_run,
    complete_run,
    settle_run,
)
from tests.core.sessions.sessions_test_support import _address


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


# Every terminal status is reported alike; a Project scope must not leak across.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "project_id"), [("completed", None), ("interrupted", "project-one")]
)
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


def test_session_list_page_is_bounded_filtered_and_keeps_required_session(manager) -> None:
    normal_ids: list[str] = []
    for index in range(5):
        session_id = f"normal-{index:02d}"
        address = _address("coder", session_id)
        manager._store.create(address, created_at=f"2026-08-01T12:{index:02d}:00+00:00")
        _classify(manager, address, {"title": f"Normal {index}", "run_kinds": ["user"]})
        # Large prompt state stays out of every summary.
        manager.ensure_prompt_pin(
            address, PINNED_SKILL_CATALOG_SLOT, {"catalog": "large" * 10_000}, lambda _pin: True
        )
        normal_ids.append(session_id)
    hidden = _address("coder", "cron-hidden")
    manager._store.create(hidden, created_at="2026-08-01T00:00:00+00:00")
    _classify(manager, hidden, {"run_kinds": ["cron"]})
    filters = SessionListFilters(
        include_subagents=False,
        include_memory_reflections=False,
        include_skill_reflections=False,
        include_cron=False,
    )

    first = manager.list_summaries_page(
        [(None, "coder")], limit=3, filters=filters, required_address=hidden
    )

    assert len(first.sessions) == 4
    assert first.total_count == 6
    assert first.next_cursor is not None
    assert first.sessions[0]["id"] == "normal-04"
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
        filters=filters,
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
    settle_run(manager, unread, "run-1", "failed", "2026-08-29T12:00:00Z")
    settle_run(manager, read, "run-2", "completed", "2026-08-29T12:01:00Z")
    manager.mark_terminal_run_read(read, "run-2")
    settle_run(manager, project, "run-3", "completed", "2026-08-29T12:02:00Z")

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
        settle_run(manager, address, f"run-{agent_id}")
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
        "librarian-user": {"run_kinds": ["user", "librarian"]},
    }
    for index, (session_id, metadata) in enumerate(metadata_by_session.items()):
        address = _address("coder", session_id)
        manager._store.create(
            address,
            created_at=f"2026-08-01T00:{index:02d}:00+00:00",
        )
        _classify(manager, address, metadata)
    # A Librarian pass of an earlier vBot ran in a Session of the Agent it curated.
    manager.create("coder", "librarian", run_kind=RunKind.LIBRARIAN)
    # Now it runs in a Session of the Librarian, labelled, titled and bound to the
    # Agent in the creating write.
    title = "Skills of Coder · 2026-10-03"
    bound = manager.create(
        "librarian",
        "pass",
        run_kind=RunKind.LIBRARIAN,
        metadata={"skill_agent_id": "coder", "auto_title": title, "auto_title_initialized": True},
    )

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
    # Sessions with a Librarian Run are never listed in another Agent's scope.
    assert listed(SessionListFilters(True, True, True, True)) == set(metadata_by_session) - {
        "librarian-user"
    }
    # The Librarian's own Sessions are listed like any conversation.
    librarian_sessions = manager.list_summaries_page(
        [(None, "librarian")], limit=100, filters=hidden
    ).sessions
    assert [(summary["id"], summary["auto_title"]) for summary in librarian_sessions] == [
        ("pass", title)
    ]
    assert manager.metadata_value(bound.address, "skill_agent_id") == "coder"


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
    "user-librarian": ({"run_kinds": ["user", "librarian"]}, "hidden"),
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
    # Revisions follow creation order, which differs from the name order here.
    assert sorted(revisions, key=lambda revision: revision.creation_order) == sorted(
        revisions, key=lambda revision: list(expected).index(revision.address.session_id)
    )


def test_history_versions_report_live_sessions_across_scopes(manager) -> None:
    live = manager.create("coder", session_id="live-one")
    live.append(ChatMessage.user("hello"))
    project = manager.create("coder", session_id="project-one", project_id="alpha")
    gone = manager.create("coder", session_id="gone")
    gone.delete()

    versions = manager.list_history_versions([live.address, project.address, gone.address])

    assert set(versions) == {live.address, project.address}
    generation_id, revision = versions[live.address]
    assert isinstance(generation_id, str) and generation_id
    assert revision >= 1


def test_subagent_links_find_live_children_of_a_parent_and_a_session_by_its_id(manager) -> None:
    parent = _address("parent", "root")
    project_parent = _address("parent", "root", "alpha")

    def child(session_id: str, subagent_id: str, linked_to: SessionAddress) -> SessionAddress:
        address: SessionAddress = manager.create("worker", session_id=session_id).address
        manager.set_metadata(
            address,
            {
                SUBAGENT_SESSION_META_KEY: True,
                SUBAGENT_PARENT_META_KEY: {
                    "id": subagent_id,
                    "agent_id": linked_to.agent_id,
                    "session_id": linked_to.session_id,
                    "project_id": linked_to.project_id,
                },
            },
        )
        return address

    # Created out of name order: children are listed oldest first.
    second = child("b-first", "sa_b", parent)
    first = child("a-second", "sa_a", parent)
    in_project = child("project-child", "sa_p", project_parent)
    child("other-child", "sa_o", _address("parent", "other"))
    gone = child("gone-child", "sa_gone", parent)
    manager.create("worker", session_id="unlinked")
    manager.get(gone).delete()

    assert manager.subagent_children(parent) == [second, first]
    assert manager.subagent_children(project_parent) == [in_project]
    assert manager.subagent_children(_address("parent", "childless")) == []
    assert manager.subagent_session("sa_a") == first
    assert manager.subagent_session("sa_p") == in_project
    assert manager.subagent_session("sa_gone") is None
    assert manager.subagent_session("sa_missing") is None


def test_live_scopes_name_each_scope_that_still_has_a_live_session(manager) -> None:
    manager.create("coder", session_id="one")
    manager.create("coder", session_id="two")
    manager.create("coder", session_id="project-one", project_id="alpha")
    manager.create("writer", session_id="draft").delete()

    assert manager.list_live_scopes() == [(None, "coder"), ("alpha", "coder")]


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
