"""Archive entries in the Session database: membership, transitions, restore and purge."""

from __future__ import annotations

import sqlite3

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_KIND_OWNER_GROUP,
    ARCHIVE_KIND_PROJECT,
    ARCHIVE_KIND_SESSION,
    ARCHIVE_STATE_ARCHIVED,
    ARCHIVE_STATE_ARCHIVING,
    ARCHIVE_STATE_PURGING,
    ARCHIVE_STATE_RESTORED,
    ARCHIVE_TREE_AGENT,
    ArchiveAddressTakenError,
    ArchiveEntry,
    ArchiveEntryBusyError,
    ArchiveEntryFilter,
    ArchiveEntryNotFoundError,
    ArchiveMembersManagedError,
    ArchiveScope,
    ArchiveTree,
    ChatSessionManager,
    SessionAddress,
)


def _states(manager: ChatSessionManager) -> dict[str, str]:
    with sqlite3.connect(manager._store.path) as connection:
        return dict(connection.execute("SELECT session_id, state FROM sessions").fetchall())


def _generations(manager: ChatSessionManager) -> list[tuple[str, str, str]]:
    with sqlite3.connect(manager._store.path) as connection:
        return sorted(connection.execute("SELECT agent_id, session_id, state FROM sessions"))


def _texts(manager: ChatSessionManager, address: SessionAddress) -> list[object]:
    return [message.content for message in manager.get(address).load_active()]


def _entry(manager: ChatSessionManager, entry_id: str) -> ArchiveEntry:
    entry = manager.archive_ledger.entry(entry_id)
    assert entry is not None
    return entry


def _agent_trees(entry_id: str) -> list[ArchiveTree]:
    return [ArchiveTree(f"archive/entries/{entry_id}/agent", ARCHIVE_TREE_AGENT, "agents/coder")]


def test_scope_archive_commits_with_session_states_and_abandon_leaves_them_live(
    manager: ChatSessionManager,
) -> None:
    ledger = manager.archive_ledger
    changes: list[None] = []
    ledger.add_changed_callback(lambda: changes.append(None))
    manager.create("coder", session_id="one")
    manager.create("coder", session_id="two")
    manager.create("coder", session_id="project", project_id="app")
    manager.create("other", session_id="three")

    abandoned = ledger.begin(ARCHIVE_KIND_AGENT, subject_id="coder", agent_id="coder")
    assert _entry(manager, abandoned.entry_id).state == ARCHIVE_STATE_ARCHIVING
    ledger.abandon(abandoned.entry_key)
    assert ledger.entry(abandoned.entry_id) is None
    assert set(_states(manager).values()) == {"live"}

    ref = ledger.begin(
        ARCHIVE_KIND_AGENT,
        subject_id="coder",
        agent_id="coder",
        facts={"name": "Coder"},
        trees=_agent_trees,
    )
    assert (
        ledger.commit_scope(
            ref.entry_key,
            ArchiveScope(agent_id="coder"),
            facts={"grants": []},
            cleanup_pending=True,
        )
        == 2
    )

    entry = _entry(manager, ref.entry_id)
    assert (entry.state, entry.session_count, entry.cleanup_pending) == (
        ARCHIVE_STATE_ARCHIVED,
        2,
        True,
    )
    assert entry.facts == {"name": "Coder", "grants": []}
    assert entry.trees == tuple(_agent_trees(ref.entry_id))
    assert [member.address.session_id for member in ledger.members(ref.entry_key)] == [
        "one",
        "two",
    ]
    assert len(ledger.members(ref.entry_key, limit=1)) == 1
    assert len(ledger.members(ref.entry_key, limit=None)) == 2
    assert _states(manager) == {
        "one": "archived",
        "two": "archived",
        "project": "live",
        "three": "live",
    }
    assert [item.entry_id for item in ledger.unsettled()] == [ref.entry_id]
    ledger.finish_cleanup(ref.entry_key)
    assert ledger.unsettled() == ()
    # The Session manager announces nothing here; the archive owner does.
    assert changes == []


def test_owner_managed_sessions_refuse_a_scope_archive_and_a_restore(
    manager: ChatSessionManager,
) -> None:
    ledger = manager.archive_ledger
    manager.create("temporary", session_id="ordinary")
    manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    )
    ref = ledger.begin(ARCHIVE_KIND_AGENT, subject_id="temporary", agent_id="temporary")

    with pytest.raises(ChatSessionError, match="managed by an Extension"):
        ledger.commit_scope(ref.entry_key, ArchiveScope(agent_id="temporary"))

    assert _entry(manager, ref.entry_id).state == ARCHIVE_STATE_ARCHIVING
    assert set(_states(manager).values()) == {"live"}

    # An Extension took over an archived member: a restore is refused as an archive error.
    ledger.abandon(ref.entry_key)
    archived = ledger.begin(ARCHIVE_KIND_AGENT, subject_id="coder", agent_id="coder")
    manager.create("coder", session_id="adopted")
    ledger.commit_scope(archived.entry_key, ArchiveScope(agent_id="coder"))
    with sqlite3.connect(manager._store.path) as connection:
        connection.execute(
            "INSERT INTO temporary_session_bindings "
            "(session_key, owner_name, group_id, participant_id, config_json) "
            "SELECT session_key, 'swarm', 'other', 'peer-2', '{}' FROM sessions "
            "WHERE session_id = 'adopted'"
        )
    ledger.begin_restore(archived.entry_id, {"target_id": None})

    with pytest.raises(ArchiveMembersManagedError, match="an Extension manages its Sessions"):
        ledger.commit_restore(archived.entry_key)

    assert _states(manager)["adopted"] == "archived"


@pytest.mark.asyncio
async def test_each_archived_session_belongs_to_exactly_one_entry(
    manager: ChatSessionManager,
) -> None:
    ledger = manager.archive_ledger
    changes: list[None] = []
    ledger.add_changed_callback(lambda: changes.append(None))
    first = manager.create("coder", session_id="one", project_id="app").address
    second = manager.create("coder", session_id="two", project_id="app").address

    session_entry = await manager.archive(first)
    project = ledger.begin(ARCHIVE_KIND_PROJECT, subject_id="app", project_id="app")
    assert ledger.commit_scope(project.entry_key, ArchiveScope(project_id="app")) == 1

    assert changes == [None]
    entry = _entry(manager, session_entry.entry_id)
    assert (entry.kind, entry.subject_id, entry.project_id, entry.agent_id) == (
        ARCHIVE_KIND_SESSION,
        "one",
        "app",
        "coder",
    )
    assert [member.address for member in ledger.members(entry.entry_key)] == [first]
    assert [member.address for member in ledger.members(project.entry_key)] == [second]
    page = ledger.page(ArchiveEntryFilter(project_id="app"), limit=1)
    assert len(page.entries) == 1 and page.next_cursor is not None
    rest = ledger.page(ArchiveEntryFilter(project_id="app"), cursor=page.next_cursor)
    assert {item.entry_id for item in page.entries + rest.entries} == {
        session_entry.entry_id,
        project.entry_id,
    }
    assert rest.next_cursor is None
    assert [item.entry_id for item in ledger.page(ArchiveEntryFilter(kind="session")).entries] == [
        session_entry.entry_id
    ]
    assert ledger.newest_entry_id(ARCHIVE_KIND_PROJECT, "app") == project.entry_id


@pytest.mark.asyncio
async def test_entry_transitions_are_compare_and_set(manager: ChatSessionManager) -> None:
    ledger = manager.archive_ledger
    address = manager.create("coder", session_id="one").address
    pending = ledger.begin(ARCHIVE_KIND_AGENT, subject_id="other", agent_id="other")
    ref = await manager.archive(address)

    with pytest.raises(ArchiveEntryNotFoundError):
        ledger.begin_restore("arc_missing", {})
    with pytest.raises(ArchiveEntryBusyError, match="archiving"):
        ledger.begin_restore(pending.entry_id, {})
    with pytest.raises(ArchiveEntryBusyError, match="archiving"):
        ledger.begin_purge(pending.entry_id)

    restoring = ledger.begin_restore(ref.entry_id, {"target_id": None})
    assert restoring.facts == {"restore_plan": {"target_id": None}}
    with pytest.raises(ArchiveEntryBusyError, match="restoring"):
        ledger.begin_restore(ref.entry_id, {})
    with pytest.raises(ArchiveEntryBusyError, match="restoring"):
        ledger.begin_purge(ref.entry_id)
    ledger.abort_restore(ref.entry_key)
    assert _entry(manager, ref.entry_id).facts == {}

    assert ledger.begin_purge(ref.entry_id).state == ARCHIVE_STATE_PURGING
    # A purging entry is resumed, never restored.
    assert ledger.begin_purge(ref.entry_id).state == ARCHIVE_STATE_PURGING
    with pytest.raises(ArchiveEntryBusyError, match="purging"):
        ledger.begin_restore(ref.entry_id, {})


@pytest.mark.asyncio
async def test_restore_rolls_back_whole_when_an_address_is_taken(
    manager: ChatSessionManager,
) -> None:
    ledger = manager.archive_ledger
    for session_id in ("one", "two"):
        manager.create("coder", session_id=session_id).append(ChatMessage.user(session_id))
    ref = ledger.begin(ARCHIVE_KIND_AGENT, subject_id="coder", agent_id="coder")
    ledger.commit_scope(ref.entry_key, ArchiveScope(agent_id="coder"))
    # Channel routing re-created one address while the Agent was archived.
    manager.create("coder", session_id="two")
    ledger.begin_restore(ref.entry_id, {"target_id": None})

    with pytest.raises(ArchiveAddressTakenError) as taken:
        ledger.commit_restore(ref.entry_key)

    assert taken.value.addresses == (SessionAddress(None, "coder", "two"),)
    assert _generations(manager) == [
        ("coder", "one", "archived"),
        ("coder", "two", "archived"),
        ("coder", "two", "live"),
    ]
    assert _entry(manager, ref.entry_id).session_count == 2
    restored = ledger.commit_restore(ref.entry_key, agent_id="coder-old")
    assert restored == (
        SessionAddress(None, "coder-old", "one"),
        SessionAddress(None, "coder-old", "two"),
    )
    assert _texts(manager, restored[1]) == ["two"]
    entry = _entry(manager, ref.entry_id)
    assert (entry.state, entry.session_count) == (ARCHIVE_STATE_RESTORED, 0)
    ledger.finish_restore(ref.entry_key)
    assert ledger.entry(ref.entry_id) is None


@pytest.mark.asyncio
async def test_restore_under_a_new_id_drops_channel_routing_and_follows_subagent_links(
    manager: ChatSessionManager,
) -> None:
    ledger = manager.archive_ledger
    routing = {
        "source_channel_id": "tg-main",
        "platform": "telegram",
        "platform_conv_id": "chat-42",
        "last_reply_target": {"message_id": 7},
    }
    parent = manager.create("coder", session_id="root").address
    manager.set_metadata(parent, {"title": "Root", **routing})
    child = manager.create("helper", session_id="child").address
    link = {
        "id": "work",
        "agent_id": "coder",
        "session_id": "root",
        "run_id": "run",
        "tool_call_id": "call",
        "tool_call_index": 0,
        "project_id": None,
    }
    manager.set_metadata(child, {"is_subagent_session": True, "subagent_parent": link})
    ref = ledger.begin(ARCHIVE_KIND_AGENT, subject_id="coder", agent_id="coder")
    ledger.commit_scope(ref.entry_key, ArchiveScope(agent_id="coder"))
    ledger.begin_restore(ref.entry_id, {"target_id": "coder-old"})

    (restored,) = ledger.commit_restore(
        ref.entry_key, agent_id="coder-old", strip_channel_keys=True
    )
    assert (
        ledger.retarget_subagent_links(
            old_project_id=None,
            old_agent_id="coder",
            new_project_id=None,
            new_agent_id="coder-old",
            session_ids=["root"],
        )
        == 1
    )

    assert manager.get_metadata(restored) == {"title": "Root"}
    assert manager.get_metadata(child)["subagent_parent"] == {**link, "agent_id": "coder-old"}


@pytest.mark.asyncio
async def test_purge_deletes_members_and_materializes_their_history_into_descendants(
    manager: ChatSessionManager,
) -> None:
    ledger = manager.archive_ledger
    source = manager.create("coder", session_id="source")
    source.append(ChatMessage.user("shared"))
    inner_fork = await manager.fork(source.address)
    live_fork = await manager.fork(source.address, target_agent_id="other")
    archived_fork = await manager.fork(source.address, target_agent_id="third")
    other_entry = await manager.archive(archived_fork.address)
    ref = ledger.begin(ARCHIVE_KIND_AGENT, subject_id="coder", agent_id="coder")
    ledger.commit_scope(ref.entry_key, ArchiveScope(agent_id="coder"))
    deleted: list[str] = []

    ledger.begin_purge(ref.entry_id)
    while True:
        before = set(_states(manager))
        if not ledger.purge_next_session(ref.entry_key):
            break
        deleted.extend(before - set(_states(manager)))
    ledger.finish_purge(ref.entry_key)

    # The fork inside the entry goes before its source.
    assert deleted == [inner_fork.id, "source"]
    assert ledger.entry(ref.entry_id) is None
    assert _texts(manager, live_fork.address) == ["shared"]
    ledger.begin_restore(other_entry.entry_id, {})
    (archived_address,) = ledger.commit_restore(other_entry.entry_key)
    assert _texts(manager, archived_address) == ["shared"]


@pytest.mark.asyncio
async def test_owner_groups_become_entries_and_leave_with_their_sessions(
    manager: ChatSessionManager,
) -> None:
    ledger = manager.archive_ledger

    def bind(group_id: str, participant_id: str) -> None:
        manager.create_bound_temporary_session(
            SessionAddress(None, f"tmp-{group_id}", participant_id),
            owner_name="swarm",
            group_id=group_id,
            participant_id=participant_id,
            config={},
        )

    bind("deleted", "peer")
    bind("deleted", "peer-2")
    bind("purged", "peer")
    manager.set_temporary_group_title(owner_name="swarm", group_id="purged", title="Docs")
    assert (
        await manager.archive_temporary_group(
            owner_name="swarm", group_id="deleted", reason="extension"
        )
        == 2
    )
    assert (
        await manager.archive_temporary_group(
            owner_name="swarm", group_id="purged", reason="extension_removed"
        )
        == 1
    )
    entries = {
        entry.subject_id: entry
        for entry in ledger.page(ArchiveEntryFilter(kind=ARCHIVE_KIND_OWNER_GROUP)).entries
    }
    assert {
        group_id: (entry.owner_name, entry.session_count, entry.facts)
        for group_id, entry in entries.items()
    } == {
        "deleted": ("swarm", 2, {"reason": "extension"}),
        "purged": ("swarm", 1, {"reason": "extension_removed"}),
    }

    assert await manager.delete_temporary_group(owner_name="swarm", group_id="deleted") == 2
    assert ledger.entry(entries["deleted"].entry_id) is None

    purged = entries["purged"]
    ledger.begin_purge(purged.entry_id)
    while ledger.purge_next_session(purged.entry_key):
        pass
    ledger.finish_purge(purged.entry_key)
    assert ledger.entry(purged.entry_id) is None
    assert (
        await manager.temporary_group_titles_async(owner_name="swarm", group_ids=["purged"]) == {}
    )
    assert _states(manager) == {}
