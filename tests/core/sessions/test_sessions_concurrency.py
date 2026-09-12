"""Tests for sessions concurrency."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.runs import RunKind
from core.sessions import (
    SESSION_RUN_KINDS_META_KEY,
    ChatSessionManager,
    SessionAddress,
    active_session_messages,
    current_skill_activation_contents,
    editable_session_message_ids,
)
from tests.core.sessions.sessions_test_support import (
    _address,
)
from tests.core.sessions.sessions_test_support import (
    manager as manager,
)


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


@pytest.mark.asyncio
async def test_generated_session_ids_skip_live_and_archived_collisions(manager, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2, 1, 2, 3))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first = manager.create("agent")
    second = manager.create("agent")
    await manager.archive(_address("agent", first.id))
    third = manager.create("agent")
    assert (first.id, second.id, third.id) == (
        "ses_000000000001",
        "ses_000000000002",
        "ses_000000000003",
    )
    assert manager.get(_address("agent", second.id)).id == second.id


@pytest.mark.asyncio
async def test_fork_allocates_a_short_id_without_reusing_an_archived_address(manager, monkeypatch):
    from core.utils import ids

    source = manager.create("agent", "ses_000000000001")
    manager.create("agent", "ses_000000000002")
    await manager.archive(_address("agent", "ses_000000000002"))
    values = iter((1, 2, 3))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    forked = await manager.fork(_address("agent", source.id))
    assert forked.id == "ses_000000000003"
    assert manager.get(_address("agent", source.id)).id == source.id


def test_parallel_generated_sessions_claim_ids_in_the_write_transaction(manager, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(lambda _: manager.create("agent"), range(2)))
    assert {session.id for session in sessions} == {"ses_000000000001", "ses_000000000002"}
