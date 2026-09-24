"""Tests for sessions lifecycle."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.chat.usage import aggregate_session_usage
from core.runs import RunKind
from core.sessions import (
    FORK_SOURCE_META_KEY,
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
)
from tests.core.sessions.sessions_test_support import (
    _address,
)
from tests.core.sessions.sessions_test_support import (
    manager as manager,
)


def test_move_updates_the_composite_address_without_losing_history(manager) -> None:
    source = manager.create("coder", session_id="session-one")
    message = ChatMessage.user("hello")
    source.append(message)
    target = _address("reviewer", source.id, "project-a")

    moved = asyncio.run(manager.move(source.address, target))

    assert not manager.exists(source.address)
    assert manager.exists(target)
    assert moved.address == target
    assert moved.load() == [message]


@pytest.mark.parametrize("restoring", [False, True])
def test_identity_reference_changes_roll_back_together(manager, monkeypatch, restoring) -> None:
    from core.sessions import _store_values

    children = [manager.create("child", session_id=f"child-{index}") for index in range(2)]
    for child in children:
        manager.set_metadata(
            child.address,
            {"subagent_parent": {"agent_id": "old", "project_id": None, "session_id": "parent"}},
        )
    updates = manager.retarget_identity_agent_references("old", "new") if restoring else ()
    before = [manager.get_metadata(child.address) for child in children]
    original = _store_values._session_metadata_storage
    calls = 0

    def fail_second_write(metadata):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected metadata write failure")
        return original(metadata)

    monkeypatch.setattr(_store_values, "_session_metadata_storage", fail_second_write)
    with pytest.raises(OSError):
        if restoring:
            manager.restore_identity_agent_references(updates)
        else:
            manager.retarget_identity_agent_references("old", "new")

    assert [manager.get_metadata(child.address) for child in children] == before


def test_identity_reference_retarget_skips_unrelated_sessions(manager) -> None:
    changed = manager.create("child", session_id="changed")
    unrelated = manager.create("child", session_id="unrelated")
    qualified = manager.create("child", session_id="qualified")
    manager.set_metadata(changed.address, {"subagent_parent": {"agent_id": "old"}})
    manager.set_metadata(
        qualified.address, {"subagent_parent": {"agent_id": "old", "project_id": "project"}}
    )
    before = [manager._store.state(session.address) for session in (unrelated, qualified)]

    updates = manager.retarget_identity_agent_references("old", "new")

    assert [update.address for update in updates] == [changed.address]
    assert manager.get_metadata(changed.address)["subagent_parent"]["agent_id"] == "new"
    assert [manager._store.state(session.address) for session in (unrelated, qualified)] == before


def test_move_reads_and_transforms_metadata_inside_its_writer_transaction(
    manager, monkeypatch
) -> None:
    source = manager.create("coder", session_id="session-one")
    target = _address("reviewer", source.id, "project-a")
    entered_store = threading.Event()
    original_move = manager._store.move

    def observed_move(*args, **kwargs):
        entered_store.set()
        return original_move(*args, **kwargs)

    monkeypatch.setattr(manager._store, "move", observed_move)
    writer = sqlite3.connect(manager._store.path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 "
            "WHERE agent_id = ? AND session_id = ? AND status = 'live'",
            (json.dumps({"title": "latest title"}), source.address.agent_id, source.id),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: asyncio.run(manager.move(source.address, target)))
            assert entered_store.wait(timeout=5)
            writer.commit()
            moved = future.result(timeout=5)
    finally:
        writer.close()

    assert manager.get_metadata(moved.address)["title"] == "latest title"


def test_fork_reads_and_transforms_metadata_inside_its_writer_transaction(
    manager, monkeypatch
) -> None:
    source = manager.create("coder", session_id="session-one")
    source.append(ChatMessage.user("hello"))
    entered_store = threading.Event()
    original_fork = manager._store.fork

    def observed_fork(*args, **kwargs):
        entered_store.set()
        return original_fork(*args, **kwargs)

    monkeypatch.setattr(manager._store, "fork", observed_fork)
    writer = sqlite3.connect(manager._store.path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE sessions SET metadata_json = ?, state_revision = state_revision + 1 "
            "WHERE agent_id = ? AND session_id = ? AND status = 'live'",
            (json.dumps({"title": "latest title"}), source.address.agent_id, source.id),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: asyncio.run(manager.fork(source.address)))
            assert entered_store.wait(timeout=5)
            writer.commit()
            forked = future.result(timeout=5)
    finally:
        writer.close()

    metadata = manager.get_metadata(forked.address)
    assert metadata["title"] == "latest title"
    assert metadata[FORK_SOURCE_META_KEY]["message_count"] == 1
    assert forked.load() == source.load()


def test_fork_titles_and_classifies_the_copy_in_its_one_write(manager, monkeypatch) -> None:
    source = manager.create("coder", session_id="session-one")
    source.append(ChatMessage.user("hello"))
    manager.set_title(source.address, "Source title")
    manager.record_run_kind(source.address, RunKind.USER)
    notified: list[object] = []
    manager.add_title_changed_callback(notified.append)
    writes: list[object] = []
    original = manager._store.database.write

    def counted(fn, **kwargs):
        writes.append(fn)
        return original(fn, **kwargs)

    monkeypatch.setattr(manager._store.database, "write", counted)

    forked = asyncio.run(
        manager.fork(
            source.address,
            strip_meta_keys=SESSION_FORK_ALWAYS_STRIP_META_KEYS,
            title="  Reviewer:   Source title ",
            run_kind=RunKind.MEMORY_REFLECTION,
        )
    )

    assert len(writes) == 1
    metadata = manager.get_metadata(forked.address)
    assert metadata["title"] == "Reviewer: Source title"
    assert metadata["run_kinds"] == ["memory_reflection"]
    assert notified == [forked.address]
    assert manager.get_metadata(source.address)["title"] == "Source title"
    # The classification reaches the list projection in the same commit.
    summary = next(item for item in manager.list_summaries("coder") if item["id"] == forked.id)
    assert summary["run_kinds"] == ["memory_reflection"]

    inherited = asyncio.run(manager.fork(source.address))
    assert manager.get_metadata(inherited.address)["title"] == "Source title"
    assert notified == [forked.address]
    with pytest.raises(ChatSessionError, match="run kind"):
        asyncio.run(manager.fork(source.address, run_kind="reflection"))


def test_archive_hides_session_until_explicit_restore(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    asyncio.run(manager.archive(address))

    assert manager.exists(address) is False
    assert manager.list("coder") == []
    with pytest.raises(ChatSessionError, match="does not exist"):
        manager.get(address)
    manager.restore(address)
    assert manager.exists(address) is True


def test_archived_address_can_start_a_fresh_generation(manager) -> None:
    address = _address("coder", "session-one")
    original = manager.create("coder", session_id=address.session_id)
    original.append(ChatMessage.user("old"))
    original_cursor = original.load_since().cursor
    asyncio.run(manager.archive(address))

    replacement = manager.get_or_create(address)
    replacement.append(ChatMessage.user("new"))

    assert [message.content for message in replacement.load()] == ["new"]
    assert replacement.load_since(original_cursor) is None
    with pytest.raises(ChatSessionError, match="live session already exists"):
        manager.restore(address)


def test_repeated_message_ids_are_preserved_in_sequence_order(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    checkpoint = ChatMessage.compaction_checkpoint(
        summary="checkpoint",
        projection=[],
        compacted_token_count=1,
        policy="automatic",
        strategy="summary",
    )

    session.append_many([checkpoint, checkpoint])

    assert session.load() == [checkpoint, checkpoint]


def test_chat_history_sql_usage_matches_canonical_python_aggregation(manager) -> None:
    session = manager.create("coder", session_id="usage-projection")
    messages = [
        ChatMessage.assistant(
            model="test",
            content="measured",
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 60,
                "cache_write_tokens": 5,
                "reasoning_tokens": 7,
            },
        ),
        ChatMessage.assistant(
            model="test",
            content="estimated input",
            usage={
                "input_tokens": 80,
                "output_tokens": 10,
                "input_tokens_estimated": True,
                "output_tokens_estimated": False,
            },
        ),
        ChatMessage.assistant(
            model="test",
            content="estimated output",
            usage={
                "input_tokens": 50,
                "output_tokens": 12,
                "cache_read_tokens": 25,
                "input_tokens_estimated": False,
                "output_tokens_estimated": True,
            },
        ),
    ]
    session.append_many(messages)

    snapshot = session.read_chat_history_snapshot(
        limit=1,
        excluded_roles=("note", "history_edit"),
    )

    assert snapshot.session_usage == aggregate_session_usage(messages)
