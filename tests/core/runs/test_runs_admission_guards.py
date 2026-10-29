"""Atomic Run-admission boundaries for Session, Agent, and Project transitions."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest

from core.runs import ChatRunManager, Run, RunAdmission, RunAdmissionBlockedError
from core.sessions import SessionAddress
from tests.core.runs.runs_test_support import finish_immediately

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("session_id", [None, "known"])
async def test_preparation_keeps_lifecycle_scopes_until_installation(
    session_id: str | None,
) -> None:
    manager = ChatRunManager()
    address = SessionAddress(None, "builder", session_id or "created")
    async with manager.prepare_admission(
        "builder", project_id=None, session_id=session_id
    ) as preparation:
        with pytest.raises(RunAdmissionBlockedError):
            async with manager.agent_admission_guard("builder", project_id=None):
                pytest.fail("the Agent is being resolved")
        with pytest.raises(RunAdmissionBlockedError):
            async with manager.session_admission_guard(address):
                pytest.fail("the Session is being prepared")
        await preparation.bind_working_project("work")
        with pytest.raises(RunAdmissionBlockedError):
            async with manager.project_admission_guard("work"):
                pytest.fail("the Working Project is reserved")
        # Preparation is a lifecycle reservation, not a Run slot or Queue item.
        run = await manager.start(
            address, finish_immediately, admission=RunAdmission(working_project_id="work")
        )
        assert await run.wait() == "done"
        assert manager.waiting_work_count() == 0
    async with manager.agent_admission_guard("builder", project_id=None):
        pass
    async with manager.project_admission_guard("work"):
        pass
    await manager.aclose()


async def test_cancelled_preparation_releases_its_lifecycle_reservation() -> None:
    manager = ChatRunManager()
    entered = asyncio.Event()

    async def prepare() -> None:
        async with manager.prepare_admission("builder", project_id=None) as preparation:
            await preparation.bind_working_project("work")
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(prepare())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with manager.agent_admission_guard("builder", project_id=None):
        pass
    async with manager.project_admission_guard("work"):
        pass
    await manager.aclose()


@pytest.mark.parametrize("guard_started", ["before", "during"])
async def test_preparation_remembers_overlapping_project_transitions(guard_started: str) -> None:
    manager = ChatRunManager()
    async with AsyncExitStack() as transition:
        if guard_started == "before":
            await transition.enter_async_context(manager.project_admission_guard("work"))
        async with manager.prepare_admission("builder", project_id=None) as preparation:
            if guard_started == "during":
                await transition.enter_async_context(manager.project_admission_guard("work"))
            # Removal and recreation can both finish before resolution returns.
            await transition.aclose()
            with pytest.raises(RunAdmissionBlockedError):
                await preparation.bind_working_project("work")
            await preparation.bind_working_project("unrelated")
    async with manager.prepare_admission("builder", project_id=None) as preparation:
        await preparation.bind_working_project("work")
    await manager.aclose()


async def test_session_guard_blocks_source_and_destination_until_release() -> None:
    manager = ChatRunManager()
    source = SessionAddress(project_id=None, agent_id="builder", session_id="session-one")
    destination = SessionAddress(project_id="vbot", agent_id="planner", session_id="session-one")

    async with manager.session_admission_guard(source, destination):
        with pytest.raises(RunAdmissionBlockedError):
            await manager.start(
                SessionAddress(project_id=None, agent_id="builder", session_id="session-one"),
                finish_immediately,
            )
        with pytest.raises(RunAdmissionBlockedError):
            await manager.enqueue(
                SessionAddress(project_id="vbot", agent_id="planner", session_id="session-one"),
                finish_immediately,
            )

        unrelated = await manager.start(
            SessionAddress(project_id=None, agent_id="writer", session_id="session-one"),
            finish_immediately,
        )
        assert await unrelated.wait() == "done"

    admitted = await manager.start(
        SessionAddress(project_id=None, agent_id="builder", session_id="session-one"),
        finish_immediately,
    )
    assert await admitted.wait() == "done"


async def test_session_guard_refuses_existing_run_and_releases_after_body_failure() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def hold(_run: Run) -> str:
        await release.wait()
        return "done"

    active = await manager.start(
        SessionAddress(project_id=None, agent_id="builder", session_id="busy"),
        hold,
    )
    with pytest.raises(RunAdmissionBlockedError):
        async with manager.session_admission_guard(
            SessionAddress(project_id=None, agent_id="builder", session_id="busy")
        ):
            pytest.fail("busy guard must not be entered")

    release.set()
    assert await active.wait() == "done"

    with pytest.raises(RuntimeError, match="storage failed"):
        async with manager.session_admission_guard(
            SessionAddress(project_id=None, agent_id="builder", session_id="idle")
        ):
            raise RuntimeError("storage failed")

    admitted = await manager.start(
        SessionAddress(project_id=None, agent_id="builder", session_id="idle"),
        finish_immediately,
    )
    assert await admitted.wait() == "done"


async def test_agent_guard_is_scoped_to_one_agent_anchor() -> None:
    manager = ChatRunManager()

    async with manager.agent_admission_guard("builder", project_id=None):
        with pytest.raises(RunAdmissionBlockedError):
            await manager.start(
                SessionAddress(project_id=None, agent_id="builder", session_id="identity"),
                finish_immediately,
            )

        project_run = await manager.start(
            SessionAddress(project_id="vbot", agent_id="builder", session_id="project"),
            finish_immediately,
        )
        other_agent_run = await manager.start(
            SessionAddress(project_id=None, agent_id="writer", session_id="identity"),
            finish_immediately,
        )
        assert await project_run.wait() == "done"
        assert await other_agent_run.wait() == "done"


async def test_project_guard_covers_anchor_and_working_project() -> None:
    manager = ChatRunManager()

    async with manager.project_admission_guard("vbot"):
        with pytest.raises(RunAdmissionBlockedError):
            await manager.start(
                SessionAddress(project_id="vbot", agent_id="builder", session_id="project-session"),
                finish_immediately,
            )
        with pytest.raises(RunAdmissionBlockedError):
            await manager.start(
                SessionAddress(project_id=None, agent_id="identity", session_id="rooted-session"),
                finish_immediately,
                admission=RunAdmission(working_project_id="vbot"),
            )

        unrelated = await manager.start(
            SessionAddress(project_id=None, agent_id="identity", session_id="other-session"),
            finish_immediately,
            admission=RunAdmission(working_project_id="other"),
        )
        assert await unrelated.wait() == "done"


async def test_project_guard_refuses_anchored_or_rooted_activity() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def hold(_run: Run) -> str:
        await release.wait()
        return "done"

    anchored = await manager.start(
        SessionAddress(project_id="vbot", agent_id="builder", session_id="project-session"),
        hold,
    )
    with pytest.raises(RunAdmissionBlockedError):
        async with manager.project_admission_guard("vbot"):
            pytest.fail("busy guard must not be entered")

    release.set()
    assert await anchored.wait() == "done"
