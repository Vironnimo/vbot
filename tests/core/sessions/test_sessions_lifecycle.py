"""Session lifecycle: create, reopen, move, fork, archive and restore.

Each Session works in the Project it was created with; move and fork carry it.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

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
from core.sessions import (
    FORK_SOURCE_META_KEY,
    SESSION_WORKING_PROJECT_META_KEY,
    ChatSessionManager,
    SessionAddress,
)
from tests.core.sessions.history_fixtures import admit_run, settle_run
from tests.core.sessions.sessions_test_support import _address


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


def _working_project(manager: ChatSessionManager, address: SessionAddress) -> str | None:
    return cast("str | None", manager.metadata_value(address, SESSION_WORKING_PROJECT_META_KEY))


def test_a_session_works_in_the_project_it_was_created_with(manager) -> None:
    defaults = {"coder": "alpha"}
    manager.set_agent_default_project(defaults.get)

    created = [
        manager.create("coder", session_id="by-default"),
        manager.create("coder", session_id="explicit", working_project_id="beta"),
        manager.create("coder", session_id="workspace", working_project_id=None),
        manager.get_or_create(_address("coder", "implicit")),
        manager.create("builder", session_id="team", project_id="team"),
    ]
    # A later change of the default moves no Session.
    defaults["coder"] = "beta"

    assert [_working_project(manager, session.address) for session in created] == [
        "alpha",
        "beta",
        None,
        "alpha",
        "team",
    ]
    summaries = {summary["id"]: summary for summary in manager.list_summaries("coder")}
    assert summaries["by-default"]["working_project_id"] == "alpha"
    assert summaries["workspace"]["working_project_id"] is None
    with pytest.raises(ChatSessionError, match="own Project"):
        manager.create("builder", session_id="other", project_id="team", working_project_id="beta")
    # The working Project is fixed: a metadata write may repeat it but not change it.
    manager.mutate_metadata(created[0].address, lambda metadata: metadata.update(title="Kept"))
    with pytest.raises(ChatSessionError, match="managed by Sessions"):
        manager.mutate_metadata(
            created[0].address, lambda metadata: metadata.update(working_project_id="beta")
        )
    assert _working_project(manager, created[0].address) == "alpha"


@pytest.mark.parametrize(
    ("source_project", "source_working", "target_project", "expected"),
    [
        pytest.param(None, "alpha", None, "alpha", id="identity-keeps-its-project"),
        pytest.param(None, None, None, None, id="identity-keeps-the-workspace"),
        pytest.param("team", None, None, "team", id="project-session-to-identity"),
        pytest.param(None, "alpha", "team", "team", id="into-a-team-the-address-governs"),
    ],
)
@pytest.mark.parametrize("operation", ["move", "fork"])
def test_move_and_fork_keep_working_where_the_source_worked(
    manager,
    operation: str,
    source_project: str | None,
    source_working: str | None,
    target_project: str | None,
    expected: str,
) -> None:
    source = manager.create(
        "coder",
        session_id="source",
        project_id=source_project,
        **({} if source_project else {"working_project_id": source_working}),
    )
    source.append(ChatMessage.user("hello"))

    if operation == "move":
        target = _address("reviewer", "source", target_project)
        result = asyncio.run(manager.move(source.address, target))
    else:
        result = asyncio.run(
            manager.fork(
                source.address, target_agent_id="reviewer", target_project_id=target_project
            )
        )

    assert _working_project(manager, result.address) == expected


def test_a_project_restored_under_a_new_id_takes_only_its_own_sessions(
    manager, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter(f"2026-08-13T21:00:0{second}.000000Z" for second in range(10))
    monkeypatch.setattr("core.sessions._store_mutations.utc_now_timestamp", lambda: next(clock))
    manager.create("coder", session_id="live", working_project_id="alpha")
    archived = manager.create("coder", session_id="archived", working_project_id="alpha")
    manager.create("coder", session_id="other", working_project_id="beta")
    archived_at = next(clock)
    # A later Project that took the old id keeps its Sessions.
    manager.create("coder", session_id="later", working_project_id="alpha")
    asyncio.run(manager.archive(archived.address))

    assert manager.retarget_working_project("alpha", "alpha-2", archived_at) == 2
    assert manager.retarget_working_project("alpha", "alpha-2", archived_at) == 0

    with sqlite3.connect(manager._store.path) as connection:
        stored = dict(connection.execute("SELECT session_id, working_project_id FROM sessions"))
    assert stored == {
        "live": "alpha-2",
        "archived": "alpha-2",
        "other": "beta",
        "later": "alpha",
    }


_AGENT_RENDERED_PINS = {
    PINNED_SKILL_CATALOG_SLOT: {"catalog_text": "coder Skills"},
    PINNED_SOUL_CONTEXT_SLOT: {"text": "coder SOUL"},
    PINNED_MEMORY_FILES_SLOT: {"text": "coder memory", "mode": "full"},
    PINNED_TOOL_DEFINITIONS_SLOT: {"v": 2, "epoch": "coder", "definitions": "[]", "sources": {}},
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


def test_fork_inherits_history_but_not_activity_or_a_stream_draft(manager) -> None:
    source = manager.create("coder", session_id="source")
    source.append_many(
        [ChatMessage.user("hello"), ChatMessage.assistant(model="test", content="hi")]
    )
    source_address = _address("coder", "source")
    settle_run(manager, source_address, "run-1")
    streaming = source.start_run("run-one")
    asyncio.run(
        streaming.append_stream_draft_async(model="test", reasoning_delta="", content_delta="half")
    )

    forked = asyncio.run(manager.fork(source_address, target_agent_id="reviewer"))

    # The fork's current view shows the inherited history; its own audit is empty.
    assert forked.load_active() == source.load_active()
    assert forked.load() == []
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


def test_archive_hides_session_until_its_entry_is_restored(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    ref = asyncio.run(manager.archive(address))

    assert manager.exists(address) is False
    assert manager.list("coder") == []
    with pytest.raises(ChatSessionError, match="does not exist"):
        manager.get(address)
    ledger = manager.archive_ledger
    ledger.begin_restore(ref.entry_id, {"target_id": None})
    assert ledger.commit_restore(ref.entry_key) == (address,)
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
