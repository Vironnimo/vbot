"""Concurrent Session writers, write locks and id allocation."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.chat import ChatMessage, ChatSessionError
from core.runs import RunKind
from core.sessions import SESSION_RUN_KINDS_META_KEY, ChatSessionManager
from tests.core.sessions.history_fixtures import admit_run
from tests.core.sessions.sessions_test_support import _address


@pytest.mark.asyncio
async def test_concurrent_run_admissions_keep_every_run_kind(manager) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    await asyncio.gather(
        admit_run(manager, address, RunKind.USER),
        admit_run(manager, address, RunKind.REFLECTION),
    )

    assert set(manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY]) == {
        RunKind.USER.value,
        RunKind.REFLECTION.value,
    }


@pytest.mark.timeout(120)  # Covers durable writes and the store's bounded SQLite contention retry.
def test_two_managers_append_concurrently_without_losing_messages(tmp_path) -> None:
    first = ChatSessionManager(tmp_path)
    second = ChatSessionManager(tmp_path)
    address = _address("coder", "session-one")
    first.create("coder", session_id=address.session_id)
    barrier = threading.Barrier(2)

    def append(manager: ChatSessionManager, prefix: str) -> None:
        session = manager.get(address)
        barrier.wait(timeout=10)  # A failed peer setup must not leave this thread waiting forever.
        for index in range(20):
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
        assert len(contents) == 40
        assert set(contents) == {
            *(f"first-{index}" for index in range(20)),
            *(f"second-{index}" for index in range(20)),
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


@pytest.mark.parametrize(
    ("agent_id", "session_id"),
    [
        ("../outside", "session-one"),
        ("coder", ""),
        ("coder", "../outside"),
        ("coder", "_leading-punctuation"),
        ("coder", "x" * 129),
    ],
)
def test_create_rejects_unsafe_agent_and_session_ids(manager, agent_id, session_id) -> None:
    with pytest.raises(ChatSessionError):
        manager.create(agent_id, session_id=session_id)


@pytest.mark.asyncio
async def test_generated_ids_skip_live_and_archived_collisions(manager, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2, 1, 2, 3, 1, 2, 3, 4))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    first = manager.create("agent")
    second = manager.create("agent")
    await manager.archive(_address("agent", first.id))
    third = manager.create("agent")
    # A fork allocates its short id in the same way.
    fork = await manager.fork(third.address)
    assert (first.id, second.id, third.id, fork.id) == (
        "ses_000000000001",
        "ses_000000000002",
        "ses_000000000003",
        "ses_000000000004",
    )
    assert manager.get(_address("agent", second.id)).id == second.id
    assert manager.get(third.address).id == third.id


def test_parallel_generated_sessions_claim_ids_in_the_write_transaction(manager, monkeypatch):
    from core.utils import ids

    values = iter((1, 1, 2))
    monkeypatch.setattr(ids.secrets, "randbits", lambda _bits: next(values))
    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(lambda _: manager.create("agent"), range(2)))
    assert {session.id for session in sessions} == {"ses_000000000001", "ses_000000000002"}
