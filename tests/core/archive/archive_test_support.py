"""An archive service over real Session, Agent and Project stores, for archive tests.

Only the surroundings the archive composes but does not own are doubles: the
Project Team resolution, Terminals, Recall and the usage import.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.agents import AGENT_FORMAT_VERSION, Agent, AgentStore
from core.archive import ArchiveService, ArchiveServices
from core.automation import AutomationReferences
from core.database import SnapshotBarrier
from core.projects import ProjectStore
from core.runs import ChatRunManager
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ARCHIVE_TREE_AGENT,
    ARCHIVE_TREE_WORKSPACE,
    ArchiveEntry,
    ArchiveTree,
    ChatSessionManager,
)
from core.utils.timestamps import utc_now_timestamp


class _Team:
    """Project Team resolution: only the listed ``(project_id, agent_id)`` pairs exist."""

    def __init__(self) -> None:
        self.members: set[tuple[str, str]] = set()

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Any:
        if (project_id or "", agent_id) not in self.members:
            raise LookupError(f"{agent_id} is not in the Team of {project_id}")
        return SimpleNamespace(id=agent_id)


class _Terminals:
    def __init__(self) -> None:
        self.closed: list[object] = []

    async def close_scope(self, owner: object) -> None:
        self.closed.append(owner)

    async def close_agent_scope(self, agent_id: str, project_id: str | None) -> None:
        self.closed.append((project_id, agent_id))

    async def close_project_scope(self, project_id: str) -> None:
        self.closed.append(project_id)


@dataclass
class ArchiveWorld:
    data_dir: Path
    sessions: ChatSessionManager
    agents: AgentStore
    projects: ProjectStore
    runs: ChatRunManager
    team: _Team
    usage_imports: list[None] = field(default_factory=list)
    recall_removals: list[tuple[str | None, str, str | None]] = field(default_factory=list)
    skill_invalidations: list[str] = field(default_factory=list)
    changes: list[None] = field(default_factory=list)
    services: ArchiveServices = field(init=False)
    service: ArchiveService = field(init=False)

    def entry(self, entry_id: str) -> ArchiveEntry:
        entry = self.sessions.archive_ledger.entry(entry_id)
        assert entry is not None
        return entry

    def payload(self, entry_id: str, name: str) -> Path:
        return self.data_dir / "archive" / "entries" / entry_id / name

    def session_rows(self, agent_id: str) -> list[tuple[str, str]]:
        """``(session_id, state)`` of every stored generation of an Agent's Sessions."""
        with sqlite3.connect(self.data_dir / "sessions.db") as connection:
            return sorted(
                connection.execute(
                    "SELECT session_id, state FROM sessions WHERE agent_id = ?", (agent_id,)
                )
            )


def agent_with_session(
    world: ArchiveWorld, agent_id: str, name: str | None = None, **settings: Any
) -> Agent:
    """Create an Agent with one Session, its current one, as its first message would.

    Agent creation itself creates no Session.
    """
    session_id = f"{agent_id}-first"
    world.agents.create(agent_id, name, **settings)
    world.sessions.create(agent_id, session_id=session_id)
    return world.agents.update(agent_id, current_session_id=session_id)


def legacy_agent_entry(world: ArchiveWorld, folder: Path) -> str:
    """An Agent an older vBot archived with its Workspace moved into ``archive/coder/``.

    ``folder``, the Workspace, held ``notes.md`` and is gone afterwards. Returns the entry id.
    """
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("mine", encoding="utf-8")
    world.agents.create("coder", "Coder", workspace=folder)
    container = world.data_dir / "archive" / "coder"
    with world.agents.archive_files("coder", container / "agent"):
        pass
    os.replace(folder, container / "workspace")
    return world.sessions.archive_ledger.adopt_payload(
        "arc_legacy",
        ARCHIVE_KIND_AGENT,
        subject_id="coder",
        archived_at=utc_now_timestamp(),
        trees=(
            ArchiveTree("archive/coder/agent", ARCHIVE_TREE_AGENT, "agents/coder"),
            ArchiveTree("archive/coder/workspace", ARCHIVE_TREE_WORKSPACE, str(folder)),
        ),
        facts={
            "name": "Coder",
            "payload_format": AGENT_FORMAT_VERSION,
            "workspace": {"path": str(folder), "external": True, "moved": True},
            "user_folders": ["archive/coder/workspace"],
        },
    ).entry_id


@pytest.fixture
def world(tmp_path: Path, current_session_store_template: Path) -> Iterator[ArchiveWorld]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ("data-store.json", "sessions.db"):
        shutil.copy2(current_session_store_template / name, data_dir)
    sessions = ChatSessionManager(data_dir)
    barrier = SnapshotBarrier()
    agents = AgentStore(data_dir, sessions=sessions, snapshot_barrier=barrier)
    projects = ProjectStore(data_dir, sessions=sessions, snapshot_barrier=barrier)
    world = ArchiveWorld(
        data_dir=data_dir,
        sessions=sessions,
        agents=agents,
        projects=projects,
        runs=ChatRunManager(persistence=sessions),
        team=_Team(),
    )
    nothing = SimpleNamespace(list_jobs=lambda: [])
    calendar = SimpleNamespace(
        actions=SimpleNamespace(list_actions=lambda: [], can_fire=lambda _action_id: True),
        list_events=lambda: [],
    )

    async def no_references(_agent_id: str) -> tuple[str, ...]:
        return ()

    async def forget_agent(agent_id: str) -> None:
        world.recall_removals.append((None, agent_id, None))

    async def forget_session(agent_id: str, session_id: str, project_id: str | None) -> None:
        world.recall_removals.append((project_id, agent_id, session_id))

    world.services = ArchiveServices(
        data_dir=data_dir,
        sessions=sessions,
        agents=agents,
        projects=projects,
        agent_resolver=cast(Any, world.team),
        runs=world.runs,
        automation=AutomationReferences(
            bootstrap=cast(Any, nothing), cron=cast(Any, nothing), calendar=cast(Any, calendar)
        ),
        terminals=cast(Any, _Terminals()),
        snapshot_barrier=barrier,
        agent_references=no_references,
        import_usage=lambda: world.usage_imports.append(None),
        remove_agent_from_recall=forget_agent,
        remove_session_from_recall=forget_session,
        invalidate_agent_skills=world.skill_invalidations.append,
        invalidate_project=lambda _project_id: None,
    )
    world.service = ArchiveService(world.services)
    world.service.add_changed_callback(lambda: world.changes.append(None))
    yield world
    agents.close()
    projects.close()
    sessions.close()
