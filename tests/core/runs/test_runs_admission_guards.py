"""Atomic Run-admission boundaries for Session, Agent, and Project transitions."""

from __future__ import annotations

from .runs_test_support import (
    ChatRunManager,
    Run,
    RunAdmissionBlockedError,
    asyncio,
    pytest,
)

pytestmark = pytest.mark.asyncio


async def _finish_immediately(_run: Run) -> str:
    return "done"


async def test_session_guard_blocks_source_and_destination_until_release() -> None:
    manager = ChatRunManager()
    source = (None, "builder", "session-one")
    destination = ("vbot", "planner", "session-one")

    async with manager.session_admission_guard(source, destination):
        with pytest.raises(RunAdmissionBlockedError, match="transitioning"):
            await manager.start(
                agent_id="builder",
                session_id="session-one",
                executor=_finish_immediately,
                project_id=None,
            )
        with pytest.raises(RunAdmissionBlockedError, match="transitioning"):
            await manager.enqueue(
                agent_id="planner",
                session_id="session-one",
                executor=_finish_immediately,
                project_id="vbot",
            )

        unrelated = await manager.start(
            agent_id="writer",
            session_id="session-one",
            executor=_finish_immediately,
            project_id=None,
        )
        assert await unrelated.wait() == "done"

    admitted = await manager.start(
        agent_id="builder",
        session_id="session-one",
        executor=_finish_immediately,
        project_id=None,
    )
    assert await admitted.wait() == "done"


async def test_session_guard_refuses_existing_run_and_releases_after_body_failure() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def hold(_run: Run) -> str:
        await release.wait()
        return "done"

    active = await manager.start(
        agent_id="builder",
        session_id="busy",
        executor=hold,
        project_id=None,
    )
    with pytest.raises(RunAdmissionBlockedError, match="active"):
        async with manager.session_admission_guard((None, "builder", "busy")):
            pytest.fail("busy guard must not be entered")

    release.set()
    assert await active.wait() == "done"

    with pytest.raises(RuntimeError, match="storage failed"):
        async with manager.session_admission_guard((None, "builder", "idle")):
            raise RuntimeError("storage failed")

    admitted = await manager.start(
        agent_id="builder",
        session_id="idle",
        executor=_finish_immediately,
        project_id=None,
    )
    assert await admitted.wait() == "done"


async def test_agent_guard_is_scoped_to_one_agent_anchor() -> None:
    manager = ChatRunManager()

    async with manager.agent_admission_guard("builder", project_id=None):
        with pytest.raises(RunAdmissionBlockedError, match="transitioning"):
            await manager.start(
                agent_id="builder",
                session_id="identity",
                executor=_finish_immediately,
                project_id=None,
            )

        project_run = await manager.start(
            agent_id="builder",
            session_id="project",
            executor=_finish_immediately,
            project_id="vbot",
        )
        other_agent_run = await manager.start(
            agent_id="writer",
            session_id="identity",
            executor=_finish_immediately,
            project_id=None,
        )
        assert await project_run.wait() == "done"
        assert await other_agent_run.wait() == "done"


async def test_project_guard_covers_anchor_and_working_project() -> None:
    manager = ChatRunManager()

    async with manager.project_admission_guard("vbot"):
        with pytest.raises(RunAdmissionBlockedError, match="transitioning"):
            await manager.start(
                agent_id="builder",
                session_id="project-session",
                executor=_finish_immediately,
                project_id="vbot",
            )
        with pytest.raises(RunAdmissionBlockedError, match="transitioning"):
            await manager.start(
                agent_id="identity",
                session_id="rooted-session",
                executor=_finish_immediately,
                project_id=None,
                working_project_id="vbot",
            )

        unrelated = await manager.start(
            agent_id="identity",
            session_id="other-session",
            executor=_finish_immediately,
            project_id=None,
            working_project_id="other",
        )
        assert await unrelated.wait() == "done"


async def test_project_guard_refuses_anchored_or_rooted_activity() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def hold(_run: Run) -> str:
        await release.wait()
        return "done"

    anchored = await manager.start(
        agent_id="builder",
        session_id="project-session",
        executor=hold,
        project_id="vbot",
    )
    with pytest.raises(RunAdmissionBlockedError, match="active"):
        async with manager.project_admission_guard("vbot"):
            pytest.fail("busy guard must not be entered")

    release.set()
    assert await anchored.wait() == "done"
