"""Working Projects for Sessions an earlier vBot created without one."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest

from core.sessions import ARCHIVE_KIND_AGENT, ArchiveScope, ChatSessionManager, SessionAddress

_MIGRATION = "sessions.0002_session_working_projects"


def _write(path: Path, content: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")


def _rerun_migration(data_dir: Path) -> dict[str, str | None]:
    """Run the migration again on the next open; return each Session's stored Project."""
    with sqlite3.connect(data_dir / "sessions.db") as connection:
        connection.execute("DELETE FROM kernel_migrations WHERE name = ?", (_MIGRATION,))
    ChatSessionManager(data_dir).close()
    with sqlite3.connect(data_dir / "sessions.db") as connection:
        rows = connection.execute("SELECT session_id, working_project_id FROM sessions")
        return dict(rows.fetchall())


def test_sessions_of_rooted_agents_keep_working_in_their_root_project(
    tmp_path: Path, current_session_store_template: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("data-store.json", "sessions.db"):
        shutil.copy2(current_session_store_template / name, tmp_path)
    manager = ChatSessionManager(tmp_path)
    for agent_id, session_id in (
        ("coder", "coder-live"),
        ("coder", "coder-archived"),
        ("gone", "gone-1"),
        ("plain", "plain-1"),
        ("retired", "retired-1"),
    ):
        manager.create(agent_id, session_id=session_id, working_project_id=None)
    manager.create("coder", session_id="coder-own", working_project_id="beta")
    manager.create("builder", session_id="team-1", project_id="alpha")
    asyncio.run(manager.archive(SessionAddress(None, "coder", "coder-archived")))
    # An archived Agent's Sessions count with the root Project its entry records.
    ledger = manager.archive_ledger
    entry = ledger.begin(
        ARCHIVE_KIND_AGENT,
        subject_id="retired",
        agent_id="retired",
        facts={"root_project_id": "alpha"},
    )
    ledger.commit_scope(entry.entry_key, ArchiveScope(agent_id="retired"))
    manager.close()
    _write(tmp_path / "agents/coder/agent.json", {"root_project_id": "alpha"})
    _write(tmp_path / "agents/gone/agent.json", {"root_project_id": "missing"})
    _write(tmp_path / "agents/plain/agent.json", {"root_project_id": None})
    _write(tmp_path / "projects/alpha/project.json", {"cwd": str(tmp_path)})

    with caplog.at_level(logging.INFO, logger="vbot.sessions"):
        stored = _rerun_migration(tmp_path)

    assert stored == {
        "coder-live": "alpha",
        "coder-archived": "alpha",
        # A Session that already works somewhere keeps it.
        "coder-own": "beta",
        # A Project Session works in its address Project and stores none.
        "team-1": None,
        # Without its root Project, a Session keeps working in the Workspace.
        "gone-1": None,
        "plain-1": None,
        "retired-1": "alpha",
    }
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == [
        "Sessions of Agents whose root Project no longer exists keep working in the "
        "Agent's Workspace (agents=gone:missing)"
    ]

    # A repeated run changes nothing, even after the Agent's root Project changed.
    _write(tmp_path / "agents/coder/agent.json", {"root_project_id": "beta"})
    _write(tmp_path / "projects/beta/project.json", {"cwd": str(tmp_path)})
    assert _rerun_migration(tmp_path) == stored
