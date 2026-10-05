"""Run queue admission, waiting-work limits, mutation, draining order and steering delivery."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

import core.runs.runs as runs_module
from core.runs import (
    RUN_STARTED_EVENT,
    ActiveRunError,
    ChatRunManager,
    QueuedRunItem,
    Run,
    RunAdmission,
    RunExecutionOwner,
    RunStatus,
    WaitingWorkLimitError,
)
from core.sessions import SessionAddress
from tests.core.runs.runs_test_support import SESSION, held

pytestmark = pytest.mark.asyncio


async def test_busy_session_rejects_start_and_an_idle_enqueue_starts_at_once() -> None:
    manager = ChatRunManager()
    active_execute, active_release = held("active")
    active_run = await manager.start(SESSION, active_execute)
    with pytest.raises(ActiveRunError):
        await manager.start(SESSION, active_execute)
    active_release.set()
    assert await active_run.wait() == "active"

    execute, release = held()
    item = await manager.enqueue(SESSION, execute, display_content="Queued hello")
    run = await item.future
    await asyncio.sleep(0)

    assert run.status == RunStatus.RUNNING
    assert manager.active_run(agent_id="coder", session_id="session-one", project_id=None) is run
    assert manager.list_queued("coder", "session-one", project_id=None) == []
    assert item.to_dict()["content"] == "Queued hello"
    [started] = [event for event in run.events if event.type == RUN_STARTED_EVENT]
    assert started.payload == {"status": RunStatus.RUNNING.value, "queue_item_id": item.item_id}
    release.set()
    assert await run.wait() == "done"


async def test_busy_session_queues_input_and_drains_it_with_its_admission() -> None:
    manager = ChatRunManager()
    active_execute, active_release = held("active")
    queued_execute, queued_release = held("queued")
    active_run = await manager.start(SESSION, active_execute)
    admission = RunAdmission(
        work_id="sub-work-one",
        contributes_to_agent_activity=False,
        source_session_id="source",
        expected_session_generation_id="generation-one",
    )
    item = await manager.enqueue(
        SESSION, queued_execute, display_content="Queued next", admission=admission
    )

    assert item.future.done() is False
    assert item.admission is admission
    assert [
        queued.item_id for queued in manager.list_queued("coder", "session-one", project_id=None)
    ] == [item.item_id]

    active_release.set()
    assert await active_run.wait() == "active"
    queued_run = await asyncio.wait_for(item.future, timeout=1)
    await asyncio.sleep(0)

    assert queued_run.status == RunStatus.RUNNING
    assert queued_run.work_id == "sub-work-one"
    assert queued_run.source_session_id == "source"
    assert queued_run.expected_session_generation_id == "generation-one"
    assert manager.list_queued("coder", "session-one", project_id=None) == []
    [started] = [event for event in queued_run.events if event.type == RUN_STARTED_EVENT]
    assert started.payload == {"status": RunStatus.RUNNING.value, "queue_item_id": item.item_id}
    queued_release.set()
    assert await queued_run.wait() == "queued"
    assert queued_run.contributes_to_agent_activity is False
    assert all(event.contributes_to_agent_activity is False for event in queued_run.events)


async def test_waiting_work_limit_rejects_the_next_queued_run(monkeypatch) -> None:
    manager = ChatRunManager(waiting_work_limit=2)
    release = asyncio.Event()
    created_items = []

    def record_item(**kwargs):
        item = QueuedRunItem(**kwargs)
        created_items.append(item)
        return item

    monkeypatch.setattr(runs_module, "QueuedRunItem", record_item)

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    active_run = await manager.start(
        SESSION,
        execute,
    )
    first = await manager.enqueue(
        SESSION,
        execute,
    )
    second = await manager.enqueue(
        SESSION,
        execute,
    )

    assert manager.waiting_work_count() == 2
    with pytest.raises(WaitingWorkLimitError):
        await manager.enqueue(
            SESSION,
            execute,
        )

    assert created_items[-1].future.cancelled()
    assert manager.waiting_work_count() == 2
    release.set()
    assert await active_run.wait() == "done"
    assert await (await first.future).wait() == "done"
    assert await (await second.future).wait() == "done"


async def test_waiting_work_admission_transfers_to_a_queued_run() -> None:
    manager = ChatRunManager(waiting_work_limit=1)
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    active_run = await manager.start(
        SESSION,
        execute,
    )
    admission = manager.reserve_waiting_work(scope="channel:chat", scope_limit=8)

    queued = await manager.enqueue(
        SESSION,
        execute,
        waiting_work_admission=admission,
    )

    assert manager.waiting_work_count() == 1
    assert manager.release_waiting_work(admission) is False
    with pytest.raises(WaitingWorkLimitError):
        manager.reserve_waiting_work(scope="other:chat", scope_limit=8)

    release.set()
    assert await active_run.wait() == "done"
    started_queued_run = await queued.future
    assert manager.waiting_work_count() == 0
    assert await started_queued_run.wait() == "done"


async def test_waiting_work_admission_enforces_its_scope_limit() -> None:
    manager = ChatRunManager(waiting_work_limit=4)

    first = manager.reserve_waiting_work(scope="channel:chat", scope_limit=2)
    second = manager.reserve_waiting_work(scope="channel:chat", scope_limit=2)

    with pytest.raises(WaitingWorkLimitError):
        manager.reserve_waiting_work(scope="channel:chat", scope_limit=2)

    assert manager.release_waiting_work(first) is True
    assert manager.release_waiting_work(second) is True


async def test_all_queued_returns_fresh_cross_session_snapshot_in_fifo_order() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    identity_run = await manager.start(
        SESSION,
        execute,
    )
    project_run = await manager.start(
        SessionAddress(project_id="project-a", agent_id="writer", session_id="session-two"),
        execute,
    )
    identity_item = await manager.enqueue(
        SESSION,
        execute,
        display_content="identity",
    )
    project_item = await manager.enqueue(
        SessionAddress(project_id="project-a", agent_id="writer", session_id="session-two"),
        execute,
        display_content="internal",
        internal=True,
    )

    snapshot = manager.all_queued()
    assert snapshot == [
        (
            SESSION,
            identity_item,
        ),
        (
            SessionAddress(project_id="project-a", agent_id="writer", session_id="session-two"),
            project_item,
        ),
    ]
    snapshot.clear()
    assert manager.all_queued() == [
        (
            SESSION,
            identity_item,
        ),
        (
            SessionAddress(project_id="project-a", agent_id="writer", session_id="session-two"),
            project_item,
        ),
    ]

    release.set()
    assert await identity_run.wait() == "done"
    assert await project_run.wait() == "done"
    assert await (await identity_item.future).wait() == "done"
    assert await (await project_item.future).wait() == "done"


async def test_multiple_enqueued_items_drain_in_fifo_order() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    started: list[str] = []
    started_events = {
        "first": asyncio.Event(),
        "second": asyncio.Event(),
        "third": asyncio.Event(),
    }
    queued_releases = {
        "first": asyncio.Event(),
        "second": asyncio.Event(),
        "third": asyncio.Event(),
    }

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    def make_executor(label: str) -> Any:
        async def execute(_run: Run) -> str:
            started.append(label)
            started_events[label].set()
            await queued_releases[label].wait()
            return label

        return execute

    active_run = await manager.start(
        SESSION,
        active_execute,
    )
    first_item = await manager.enqueue(
        SESSION,
        make_executor("first"),
        display_content="first",
    )
    second_item = await manager.enqueue(
        SESSION,
        make_executor("second"),
        display_content="second",
    )
    third_item = await manager.enqueue(
        SESSION,
        make_executor("third"),
        display_content="third",
    )

    assert [
        item.display_content
        for item in manager.list_queued("coder", "session-one", project_id=None)
    ] == [
        "first",
        "second",
        "third",
    ]

    active_release.set()
    assert await active_run.wait() == "active"

    first_run = await asyncio.wait_for(first_item.future, timeout=1)
    await started_events["first"].wait()
    assert started == ["first"]
    queued_releases["first"].set()
    assert await first_run.wait() == "first"

    second_run = await asyncio.wait_for(second_item.future, timeout=1)
    await started_events["second"].wait()
    assert started == ["first", "second"]
    queued_releases["second"].set()
    assert await second_run.wait() == "second"

    third_run = await asyncio.wait_for(third_item.future, timeout=1)
    await started_events["third"].wait()
    assert started == ["first", "second", "third"]
    queued_releases["third"].set()
    assert await third_run.wait() == "third"


async def test_remove_queued_item_cancels_future_and_removes_from_queue() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        return "queued"

    active_run = await manager.start(
        SESSION,
        active_execute,
    )
    item = await manager.enqueue(
        SESSION,
        queued_execute,
        display_content="remove me",
    )

    assert manager.remove_queued("coder", "session-one", item.item_id, project_id=None) is True
    assert manager.list_queued("coder", "session-one", project_id=None) == []
    assert item.future.cancelled() is True
    assert manager.remove_queued("coder", "session-one", item.item_id, project_id=None) is False

    active_release.set()
    assert await active_run.wait() == "active"
    assert manager.active_run(agent_id="coder", session_id="session-one", project_id=None) is None


async def test_cancelling_queue_waiter_removes_item_and_prevents_execution() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_executed = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        queued_executed.set()
        return "queued"

    active_run = await manager.start(
        SESSION,
        active_execute,
    )
    item = await manager.enqueue(
        SESSION,
        queued_execute,
        display_content="abandoned",
    )

    async def wait_for_start() -> Run:
        return await item.future

    waiter = asyncio.create_task(wait_for_start())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)

    assert item.future.cancelled() is True
    assert manager.list_queued("coder", "session-one", project_id=None) == []

    active_release.set()
    assert await active_run.wait() == "active"
    await asyncio.sleep(0)

    assert queued_executed.is_set() is False
    assert manager.active_run(agent_id="coder", session_id="session-one", project_id=None) is None


async def test_queue_drain_skips_future_cancelled_in_same_tick() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_executed = asyncio.Event()
    queued_item: QueuedRunItem | None = None

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        assert queued_item is not None
        queued_item.future.cancel()
        return "active"

    async def queued_execute(_run: Run) -> str:
        queued_executed.set()
        return "queued"

    active_run = await manager.start(
        SESSION,
        active_execute,
    )
    queued_item = await manager.enqueue(
        SESSION,
        queued_execute,
        display_content="cancel during drain",
    )

    active_release.set()
    assert await active_run.wait() == "active"
    await asyncio.sleep(0)

    assert queued_item.future.cancelled() is True
    assert queued_executed.is_set() is False
    assert manager.list_queued("coder", "session-one", project_id=None) == []


async def test_unexpected_drain_failure_resolves_item_and_keeps_draining(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken_work_id = "broken"

    def admission_validator(_address: SessionAddress, admission: RunAdmission) -> None:
        if admission.work_id == broken_work_id and active_release.is_set():
            raise RuntimeError("validator exploded")

    manager = ChatRunManager(admission_validator=admission_validator)
    active_release = asyncio.Event()
    address = SESSION

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        return "queued"

    active_run = await manager.start(address, active_execute)
    broken_item = await manager.enqueue(
        address,
        queued_execute,
        admission=RunAdmission(work_id=broken_work_id),
    )
    healthy_item = await manager.enqueue(address, queued_execute)

    with caplog.at_level(logging.ERROR, logger="vbot.runs"):
        active_release.set()
        assert await active_run.wait() == "active"
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="validator exploded"):
        broken_item.future.result()
    healthy_run = await asyncio.wait_for(healthy_item.future, 1)
    assert await healthy_run.wait() == "queued"
    assert manager.list_queued("coder", "session-one", project_id=None) == []
    assert any(
        record.levelno == logging.ERROR and record.exc_info is not None for record in caplog.records
    )


async def test_update_queued_item_replaces_executor_and_display_content() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    updated_started = asyncio.Event()
    queued_release = asyncio.Event()
    executed: list[str] = []

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def original_execute(_run: Run) -> str:
        executed.append("original")
        return "original"

    async def updated_execute(_run: Run) -> str:
        executed.append("updated")
        updated_started.set()
        await queued_release.wait()
        return "updated"

    active_run = await manager.start(
        SESSION,
        active_execute,
    )
    item = await manager.enqueue(
        SESSION,
        original_execute,
        display_content="original",
        editable=True,
    )

    assert (
        manager.update_queued(
            "coder",
            "session-one",
            item.item_id,
            updated_execute,
            "updated",
            project_id=None,
            editable=False,
        )
        is True
    )
    assert (
        manager.list_queued("coder", "session-one", project_id=None)[0].display_content == "updated"
    )
    assert manager.list_queued("coder", "session-one", project_id=None)[0].editable is False
    assert (
        manager.update_queued(
            "coder", "session-one", "missing", updated_execute, "updated", project_id=None
        )
        is False
    )

    active_release.set()
    assert await active_run.wait() == "active"

    queued_run = await asyncio.wait_for(item.future, timeout=1)
    await updated_started.wait()
    assert executed == ["updated"]
    queued_release.set()
    assert await queued_run.wait() == "updated"


async def test_steering_append_failure_retains_input_and_blocks_mid_append_edits() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()
    address = SessionAddress(project_id=None, agent_id="coder", session_id="one")

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await manager.start(address, execute)
    await asyncio.sleep(0)
    item = await manager.enqueue(address, execute, steerable=True, editable=True)
    manager.steer_queued("coder", "one", item.item_id, project_id=None)

    async def fail_append(_item: QueuedRunItem) -> None:
        assert not manager.remove_queued("coder", "one", item.item_id, project_id=None)
        assert not manager.update_queued(
            "coder", "one", item.item_id, execute, "Changed", project_id=None
        )
        raise OSError("append failed")

    try:
        with pytest.raises(OSError):
            await manager.deliver_steering(run, fail_append)
        assert manager.pending_steering(run) == [item]
        assert not item.future.done()
        assert manager.remove_queued("coder", "one", item.item_id, project_id=None)
    finally:
        await manager.aclose()


@pytest.mark.parametrize("cleared", [True, False], ids=["queue-cleared", "queue-kept"])
async def test_cancel_during_a_steering_append_starts_a_successor_only_for_kept_input(
    cleared: bool,
) -> None:
    manager = ChatRunManager()
    address = SessionAddress(project_id=None, agent_id="coder", session_id="one")
    steered = asyncio.Event()
    appending = asyncio.Event()

    async def append(_item: QueuedRunItem) -> None:
        appending.set()
        await asyncio.Event().wait()

    async def execute(run: Run) -> str:
        await steered.wait()
        await manager.deliver_steering(run, append)
        return "done"

    successor_started = asyncio.Event()

    async def successor(_run: Run) -> str:
        successor_started.set()
        return "successor"

    run = await manager.start(address, execute)
    item = await manager.enqueue(address, successor, steerable=True)
    manager.steer_queued("coder", "one", item.item_id, project_id=None)
    steered.set()
    try:
        await asyncio.wait_for(appending.wait(), timeout=1)
        if cleared:
            assert manager.clear_queued("coder", "one", project_id=None) == 1
        await manager.cancel(run.id)
        for _ in range(5):
            await asyncio.sleep(0)

        assert run.status == RunStatus.CANCELLED
        assert successor_started.is_set() is not cleared
        if cleared:
            assert manager.list_queued("coder", "one", project_id=None) == []
            assert item.future.cancelled()
    finally:
        await manager.aclose()


async def test_removing_second_steer_during_first_append_does_not_deliver_it() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()
    address = SessionAddress(project_id=None, agent_id="coder", session_id="one")

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await manager.start(address, execute)
    await asyncio.sleep(0)
    first = await manager.enqueue(address, execute, steerable=True)
    second = await manager.enqueue(address, execute, steerable=True)
    for item in [first, second]:
        manager.steer_queued("coder", "one", item.item_id, project_id=None)
    delivered = []

    async def append(item: QueuedRunItem) -> None:
        delivered.append(item.item_id)
        assert manager.remove_queued("coder", "one", second.item_id, project_id=None)
        await asyncio.sleep(0)

    try:
        assert await manager.deliver_steering(run, append)
        assert delivered == [first.item_id]
        assert first.future.result() is run
        assert second.future.cancelled()
    finally:
        await manager.aclose()


async def test_selected_input_no_run_took_starts_first_in_queue_order() -> None:
    manager = ChatRunManager()
    active_execute, active_release = held("active")
    active_run = await manager.start(SESSION, active_execute)
    ordinary, first, second = [
        await manager.enqueue(SESSION, held(label)[0], steerable=True, display_content=label)
        for label in ["ordinary", "first", "second"]
    ]
    for item in [second, first]:
        manager.steer_queued("coder", "session-one", item.item_id, project_id=None)

    try:
        active_release.set()
        assert await active_run.wait() == "active"
        successor = await asyncio.wait_for(first.future, timeout=1)
        queued = manager.list_queued("coder", "session-one", project_id=None)
        assert [item.display_content for item in queued] == ["second", "ordinary"]
        assert manager.pending_steering(successor) == [second]
        assert not ordinary.steering
    finally:
        await manager.aclose()


@pytest.mark.parametrize(
    "admission",
    [
        RunAdmission(owner=RunExecutionOwner("swarm", "group", "member", "gen", "epoch")),
        RunAdmission(working_project_id="other"),
    ],
    ids=["extension-owned-run", "other-working-project"],
)
async def test_input_an_active_run_cannot_take_stays_selected(admission: RunAdmission) -> None:
    manager = ChatRunManager()
    execute, _release = held()
    run = await manager.start(SESSION, execute, admission=admission)
    item = await manager.enqueue(SESSION, execute, steerable=True)
    manager.steer_queued("coder", "session-one", item.item_id, project_id=None)
    try:
        assert manager.pending_steering(run) == []
        assert item.steering
    finally:
        await manager.aclose()
