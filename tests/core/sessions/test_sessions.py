"""Public Session facade tests for canonical SQLite persistence."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.runs import RunKind
from core.sessions import (
    FORK_SOURCE_META_KEY,
    PROMPT_CACHE_AFFINITY_META_KEY,
    SESSION_RUN_KINDS_META_KEY,
    ChatSessionManager,
    SessionAddress,
    active_session_messages,
    current_skill_activation_contents,
    editable_session_message_ids,
)


def _address(agent_id: str, session_id: str, project_id: str | None = None) -> SessionAddress:
    return SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)


@pytest.fixture
def manager(tmp_path):
    sessions = ChatSessionManager(tmp_path)
    yield sessions
    sessions.close()


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


def test_metadata_activity_and_continuation_change_state_not_history(manager) -> None:
    address = _address("coder", "session-one")
    session = manager.create("coder", session_id=address.session_id)
    session.append(ChatMessage.user("hello"))
    revision = manager.history_revision(address)

    manager.set_metadata(address, {"project": "vbot"})
    manager.record_run_kind(address, RunKind.USER)
    manager.record_terminal_run(address, "run-1", "completed", "2026-08-29T12:00:00Z")
    session.append_continuation_records([{"turn": 1}])

    assert manager.history_revision(address) == revision
    assert manager.get_metadata(address)["project"] == "vbot"
    assert manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY] == [RunKind.USER.value]
    assert session.load_continuation_records() == [{"turn": 1}]
    assert manager.mark_terminal_run_read(address, "wrong")["marked_read"] is False
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is True


def test_fork_copies_history_but_not_activity_or_continuation(manager) -> None:
    source = manager.create("coder", session_id="source")
    source.append_many(
        [ChatMessage.user("hello"), ChatMessage.assistant(model="test", content="hi")]
    )
    source.append_continuation_record({"turn": 1})
    source_address = _address("coder", "source")
    manager.record_terminal_run(source_address, "run-1", "completed", "2026-08-29T12:00:00Z")

    forked = asyncio.run(manager.fork(source_address, target_agent_id="reviewer"))

    assert forked.load() == source.load()
    assert forked.load_continuation_records() == []
    assert manager.list_completion_activity("reviewer")[0]["has_unread_completion"] is False
    metadata = manager.get_metadata(forked.address)
    assert metadata[FORK_SOURCE_META_KEY]["session_id"] == "source"
    assert metadata[PROMPT_CACHE_AFFINITY_META_KEY] != manager.prompt_cache_affinity_id(
        source_address
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


def test_concurrent_metadata_mutations_do_not_overwrite_each_other(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(manager.record_run_kind, address, RunKind.USER),
            pool.submit(manager.record_run_kind, address, RunKind.REFLECTION),
        ]
        for future in futures:
            future.result()

    assert set(manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY]) == {
        RunKind.USER.value,
        RunKind.REFLECTION.value,
    }


def test_two_managers_append_concurrently_without_losing_messages(tmp_path) -> None:
    first = ChatSessionManager(tmp_path)
    second = ChatSessionManager(tmp_path)
    address = _address("coder", "session-one")
    first.create("coder", session_id=address.session_id)
    barrier = threading.Barrier(2)

    def append(manager: ChatSessionManager, prefix: str) -> None:
        session = manager.get(address)
        barrier.wait()
        for index in range(40):
            session.append(ChatMessage.user(f"{prefix}-{index}"))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(append, first, "first"),
                pool.submit(append, second, "second"),
            ]
            for future in futures:
                future.result()

        contents = [message.content for message in first.get(address).load()]
        assert len(contents) == 80
        assert set(contents) == {
            *(f"first-{index}" for index in range(40)),
            *(f"second-{index}" for index in range(40)),
        }
    finally:
        second.close()
        first.close()


def test_two_managers_get_or_create_one_live_generation(tmp_path) -> None:
    first = ChatSessionManager(tmp_path)
    second = ChatSessionManager(tmp_path)
    address = _address("coder", "session-one")
    barrier = threading.Barrier(2)

    def create(manager: ChatSessionManager):
        barrier.wait()
        return manager.get_or_create(address)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            handles = [
                pool.submit(create, first),
                pool.submit(create, second),
            ]
            assert [future.result().address for future in handles] == [address, address]
        assert [session.id for session in first.list("coder")] == [address.session_id]
    finally:
        second.close()
        first.close()


def test_callback_failure_does_not_turn_a_committed_title_into_an_error(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    def fail(_address: SessionAddress) -> None:
        raise RuntimeError("observer failed")

    manager.add_title_changed_callback(fail)

    assert manager.set_title(address, "Persisted") == "Persisted"
    assert manager.get_metadata(address)["title"] == "Persisted"


def test_deferred_notes_keep_their_existing_ordering(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    session.begin_defer_notes()
    session.add_note("first")
    session.add_note("second")

    session.flush_deferred_notes()

    assert [message.content for message in session.load()] == ["first", "second"]
    assert [message.content for message in session.drain_pending_notes()] == ["first", "second"]


def test_history_edits_and_skill_cache_preserve_chat_semantics(manager) -> None:
    session = manager.create("coder", session_id="session-one")
    user = ChatMessage.user("first")
    replacement = ChatMessage.user("replacement")
    session.append_many([user, ChatMessage.history_edit(user.id), replacement])

    assert active_session_messages(session.load()) == [replacement]
    assert editable_session_message_ids(session.load()) == frozenset({replacement.id})
    assert current_skill_activation_contents(session.load()) == {}


def test_write_lock_is_reentrant_for_child_tasks(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    async def scenario() -> None:
        async with manager.write_lock(address):

            async def child() -> None:
                async with manager.write_lock(address):
                    manager.get(address).append(ChatMessage.note("child"))

            await asyncio.create_task(child())

    asyncio.run(scenario())
    assert [message.content for message in manager.get(address).load()] == ["child"]


@pytest.mark.parametrize("agent_id", ["", "../outside", "agent name"])
def test_create_rejects_invalid_agent_ids(manager, agent_id) -> None:
    with pytest.raises(ChatSessionError):
        manager.create(agent_id, session_id="session-one")


@pytest.mark.parametrize("session_id", ["", "../outside", "name.jsonl", ".hidden"])
def test_create_rejects_invalid_session_ids(manager, session_id) -> None:
    with pytest.raises(ChatSessionError):
        manager.create("coder", session_id=session_id)
