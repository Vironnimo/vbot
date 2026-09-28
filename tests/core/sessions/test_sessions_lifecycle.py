"""Session lifecycle: create, reopen, move, fork, archive and restore."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.prompts.pinned_context import (
    PINNED_MEMORY_FILES_SLOT,
    PINNED_SKILL_CATALOG_SLOT,
    PINNED_SOUL_CONTEXT_SLOT,
    PINNED_TOOL_DEFINITIONS_SLOT,
    PINNED_WORKING_PROJECT_CONTEXT_SLOT,
)
from core.runs import RunKind
from core.sessions import FORK_SOURCE_META_KEY, ChatSessionManager
from tests.core.sessions.history_fixtures import admit_run, settle_run
from tests.core.sessions.sessions_test_support import _address, _continuation_start


def test_committed_message_survives_a_fresh_runtime_open(tmp_path) -> None:
    address = _address("agent", "restart")
    sessions = ChatSessionManager(tmp_path)
    try:
        sessions.create("agent", session_id=address.session_id).append(ChatMessage.user("hello"))
    finally:
        sessions.close()

    reopened = ChatSessionManager(tmp_path)
    try:
        assert [message.content for message in reopened.get(address).load()] == ["hello"]
    finally:
        reopened.close()


def test_get_or_create_reads_an_existing_session_without_a_write(manager, monkeypatch) -> None:
    address = _address("coder", "existing")
    writes: list[object] = []
    original = manager._store.database.write

    def counted(fn, **kwargs):
        writes.append(fn)
        return original(fn, **kwargs)

    monkeypatch.setattr(manager._store.database, "write", counted)

    created = manager.get_or_create(address)
    assert len(writes) == 1
    assert manager.get_or_create(address).address == created.address
    assert len(writes) == 1


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


_AGENT_RENDERED_PINS = {
    PINNED_SKILL_CATALOG_SLOT: {"catalog_text": "coder Skills"},
    PINNED_SOUL_CONTEXT_SLOT: {"text": "coder SOUL"},
    PINNED_MEMORY_FILES_SLOT: {"text": "coder memory", "mode": "full"},
    PINNED_TOOL_DEFINITIONS_SLOT: {"v": 1, "epoch": "coder", "definitions": [], "sources": {}},
}


@pytest.mark.parametrize("operation", ["same-agent fork", "cross-agent fork", "cross-agent move"])
def test_agent_rendered_pins_never_reach_another_agent(manager, operation) -> None:
    source = manager.create("coder", session_id="pinned")
    source.append(ChatMessage.user("hello"))
    project_pin = {"text": "Project context", "working_project_id": None}
    pins = {**_AGENT_RENDERED_PINS, PINNED_WORKING_PROJECT_CONTEXT_SLOT: project_pin}
    for slot, value in pins.items():
        manager.ensure_prompt_pin(source.address, slot, value, lambda _pin: True)

    if operation == "same-agent fork":
        target = asyncio.run(manager.fork(source.address)).address
    elif operation == "cross-agent fork":
        target = asyncio.run(manager.fork(source.address, target_agent_id="reviewer")).address
    else:
        target = asyncio.run(manager.move(source.address, _address("reviewer", source.id))).address

    carried = {slot: manager.prompt_pin(target, slot) for slot in pins}
    if operation == "same-agent fork":
        assert carried == pins
    else:
        # The reviewer renders its own Skill catalog, SOUL, memory and Tools; only
        # Project-qualified state, which re-renders on its own, may carry over.
        assert carried == {
            **dict.fromkeys(_AGENT_RENDERED_PINS),
            PINNED_WORKING_PROJECT_CONTEXT_SLOT: project_pin,
        }


@pytest.mark.parametrize("operation", ["move", "fork"])
def test_move_and_fork_read_metadata_inside_their_writer_transaction(
    manager, monkeypatch, operation
) -> None:
    source = manager.create("coder", session_id="session-one")
    source.append(ChatMessage.user("hello"))
    operations: dict[str, Callable[[], Any]] = {
        "move": lambda: asyncio.run(
            manager.move(source.address, _address("reviewer", source.id, "project-a"))
        ),
        "fork": lambda: asyncio.run(manager.fork(source.address)),
    }
    entered_store = threading.Event()
    original = getattr(manager._store, operation)

    def observed(*args, **kwargs):
        entered_store.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(manager._store, operation, observed)
    writer = sqlite3.connect(manager._store.path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE sessions SET title = ?, state_revision = state_revision + 1 "
            "WHERE agent_id = ? AND session_id = ? AND state = 'live'",
            ("latest title", source.address.agent_id, source.id),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(operations[operation])
            assert entered_store.wait(timeout=5)
            writer.commit()
            result = future.result(timeout=5)
    finally:
        writer.close()

    assert manager.get_metadata(result.address)["title"] == "latest title"


def test_fork_inherits_history_but_not_activity_or_continuation(manager) -> None:
    source = manager.create("coder", session_id="source")
    source.append_many(
        [ChatMessage.user("hello"), ChatMessage.assistant(model="test", content="hi")]
    )
    source_address = _address("coder", "source")
    settle_run(manager, source_address, "run-1")
    source.start_run("run-one")
    source.append_continuation_record(_continuation_start())

    forked = asyncio.run(manager.fork(source_address, target_agent_id="reviewer"))

    # The fork's current view shows the inherited history; its own audit is empty.
    assert forked.load_active() == source.load_active()
    assert forked.load() == []
    assert forked.load_continuation() is None
    # The latest completion is the source's own; the fork completed no Run, and
    # an inherited Run's id marks nothing read there.
    assert manager.list_completion_activity([(None, "reviewer")]) == {(None, "reviewer"): []}
    assert manager.mark_terminal_run_read(forked.address, "run-1")["marked_read"] is False
    source_activity = manager.list_completion_activity([(None, "coder")])[(None, "coder")]
    assert [row["unread_run_id"] for row in source_activity] == ["run-1"]
    metadata = manager.get_metadata(forked.address)
    assert metadata[FORK_SOURCE_META_KEY] == {
        "agent_id": "coder",
        "session_id": "source",
        "project_id": None,
        "forked_at": metadata[FORK_SOURCE_META_KEY]["forked_at"],
    }
    # A fork into another Agent's scope starts its own prompt-cache lineage.
    assert manager.prompt_cache_affinity_id(forked.address) != manager.prompt_cache_affinity_id(
        source_address
    )


def test_fork_titles_and_classifies_the_copy_in_its_one_write(manager, monkeypatch) -> None:
    source = manager.create("coder", session_id="session-one")
    source.append(ChatMessage.user("hello"))
    manager.set_title(source.address, "Source title")
    asyncio.run(admit_run(manager, source.address))
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
