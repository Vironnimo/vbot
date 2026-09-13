import asyncio

import pytest

from core.runs import ChatRunManager, RunAdmissionBlockedError
from core.sessions import SessionAddress


@pytest.mark.asyncio
async def test_maintenance_rejects_new_work_but_drains_accepted_queue() -> None:
    manager = ChatRunManager()
    address = SessionAddress(project_id=None, agent_id="main", session_id="session-one")
    release = asyncio.Event()

    async def active(_run):
        await release.wait()
        return "active"

    async def queued(_run):
        return "queued"

    first = await manager.start(address, active)
    accepted = await manager.enqueue(address, queued)
    status = await manager.maintenance_begin("operation-one")
    assert status["queued_count"] == 1

    with pytest.raises(RunAdmissionBlockedError):
        await manager.start(address, queued)
    with pytest.raises(RunAdmissionBlockedError):
        await manager.enqueue(address, queued)

    release.set()
    assert await first.wait() == "active"
    second = await accepted.future
    assert await second.wait() == "queued"
    assert (await manager.maintenance_status("operation-one"))["safe_to_stop"] is True


@pytest.mark.asyncio
async def test_acknowledged_origin_blocks_safe_stop_until_cancellation_finishes() -> None:
    manager = ChatRunManager()
    address = SessionAddress(project_id=None, agent_id="main", session_id="session-one")
    release = asyncio.Event()

    async def active(_run):
        _run.add_cancel_callback(lambda: release.set())
        await release.wait()

    run = await manager.start(address, active)
    status = await manager.maintenance_begin("operation-one", origin=(address, run.id))
    assert status["origin_pending"] is True
    assert status["safe_to_stop"] is False
    await manager.cancel(run.id, reason="application_update")
    assert (await manager.maintenance_status("operation-one"))["safe_to_stop"] is True


@pytest.mark.asyncio
async def test_origin_cancel_cleanup_blocks_safe_stop() -> None:
    manager = ChatRunManager()
    address = SessionAddress(project_id=None, agent_id="main", session_id="session-one")
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def active(run):
        async def cleanup():
            cleanup_started.set()
            await cleanup_release.wait()

        run.add_cancel_callback(cleanup)
        await asyncio.Event().wait()

    run = await manager.start(address, active)
    await manager.maintenance_begin("operation-one", origin=(address, run.id))
    cancelling = asyncio.create_task(manager.cancel(run.id, reason="application_update"))
    await cleanup_started.wait()
    assert (await manager.maintenance_status("operation-one"))["safe_to_stop"] is False
    cleanup_release.set()
    await cancelling
    assert (await manager.maintenance_status("operation-one"))["safe_to_stop"] is True


@pytest.mark.asyncio
async def test_origin_cancellation_allows_same_session_accepted_queue_to_drain() -> None:
    manager = ChatRunManager()
    address = SessionAddress(project_id=None, agent_id="main", session_id="session-one")
    origin_wait = asyncio.Event()
    queued_finished = asyncio.Event()

    async def origin(run):
        run.add_cancel_callback(lambda: origin_wait.set())
        await origin_wait.wait()

    async def queued(_run):
        queued_finished.set()

    run = await manager.start(address, origin)
    accepted = await manager.enqueue(address, queued)
    await manager.maintenance_begin("operation-one", origin=(address, run.id))

    await manager.cancel(run.id, reason="application_update")
    drained = await accepted.future
    await drained.wait()

    assert queued_finished.is_set()
    assert (await manager.maintenance_status("operation-one"))["safe_to_stop"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True])
async def test_maintenance_drains_previously_reserved_work(busy):
    from dataclasses import replace

    manager = ChatRunManager()
    address = SessionAddress(project_id=None, agent_id="main", session_id="reserved-work")
    release = asyncio.Event()

    async def active(_run):
        await release.wait()

    async def incoming(_run):
        return "reserved-work-completed"

    first = await manager.start(address, active) if busy else None
    reservation = manager.reserve_waiting_work(scope="channel:test", scope_limit=4)
    await manager.maintenance_begin("update-one")
    assert (await manager.maintenance_status("update-one"))["safe_to_stop"] is False
    with pytest.raises(RunAdmissionBlockedError):
        manager.reserve_waiting_work(scope="channel:test", scope_limit=4)
    with pytest.raises(RunAdmissionBlockedError):
        await manager.enqueue(
            address, incoming, waiting_work_admission=replace(reservation, id="invalid")
        )
    accepted = await manager.enqueue(address, incoming, waiting_work_admission=reservation)
    assert manager.waiting_work_count() == (1 if busy else 0)
    with pytest.raises(RunAdmissionBlockedError):
        await manager.enqueue(address, incoming, waiting_work_admission=reservation)
    release.set()
    if first:
        await first.wait()
    run = await accepted.future
    assert await run.wait() == "reserved-work-completed"
    assert (await manager.maintenance_status("update-one"))["safe_to_stop"] is True
    await manager.aclose()
