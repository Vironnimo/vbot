"""Atomic Run-admission boundaries for Session, Agent, and Project transitions."""

from __future__ import annotations

import asyncio

import pytest

from core.runs import ChatRunManager, Run, RunAdmission, RunAdmissionBlockedError
from core.sessions import SessionAddress
from tests.core.runs.runs_test_support import finish_immediately

pytestmark = pytest.mark.asyncio


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
