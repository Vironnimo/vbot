"""The working Project of Sessions an earlier vBot created without one.

An earlier vBot stored no working Project: a Session of an Identity Agent
worked in whatever Project the Agent was rooted in (``root_project_id`` in its
``agent.json``) at each Run. The Session database migration
``sessions.0002_session_working_projects`` keeps that once: each Session of a
rooted Agent, live or archived, gets the Agent's current root Project as its
working Project. An Agent in the archive counts with the root Project its
archive entry records, so its Sessions work there again after a restore.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from core.sessions._archive_types import ARCHIVE_KIND_AGENT, ARCHIVE_STATE_ARCHIVED
from core.utils.file_status import is_dir_strict, is_file_strict
from core.utils.ids import has_id_entry
from core.utils.logging import get_logger

_LOGGER = get_logger("sessions")


def adopt_agent_root_projects(connection: sqlite3.Connection, data_dir: Path) -> None:
    """Give the Sessions of rooted Identity Agents their Agent's root Project.

    Only Sessions without a working Project change, so a repeated run changes
    nothing more. A root Project that no longer exists is skipped with one
    warning: those Sessions keep working in their Agent's Workspace, as they did.
    """
    sessions = 0
    agents: list[str] = []
    missing: list[str] = []
    for agent_id, project_id, where, parameters in _rooted_agents(connection, data_dir):
        if not _project_exists(data_dir, project_id):
            missing.append(f"{agent_id}:{project_id}")
            continue
        cursor = connection.execute(
            "UPDATE sessions SET working_project_id = ? "
            f"WHERE project_id = '' AND working_project_id IS NULL AND {where}",
            (project_id, *parameters),
        )
        if cursor.rowcount:
            sessions += int(cursor.rowcount)
            agents.append(agent_id)
    if missing:
        _LOGGER.warning(
            "Sessions of Agents whose root Project no longer exists keep working in the "
            "Agent's Workspace (agents=%s)",
            ",".join(missing),
        )
    if sessions:
        _LOGGER.info(
            "Set the working Project of Sessions to their Agent's root Project "
            "(agents=%s sessions=%d)",
            ",".join(agents),
            sessions,
        )


def _rooted_agents(
    connection: sqlite3.Connection, data_dir: Path
) -> list[tuple[str, str, str, tuple[Any, ...]]]:
    """Each rooted Agent with its root Project and the predicate of its Sessions.

    Live Agents own their Identity Sessions by Agent id; an archived Agent owns
    the members of its archive entry.
    """
    rooted: list[tuple[str, str, str, tuple[Any, ...]]] = []
    agents_dir = data_dir / "agents"
    if is_dir_strict(agents_dir):
        for agent_dir in sorted(agents_dir.iterdir(), key=lambda entry: entry.name):
            if not is_dir_strict(agent_dir):
                continue
            project_id = _root_project(_document(agent_dir / "agent.json"))
            if project_id is not None:
                rooted.append((agent_dir.name, project_id, "agent_id = ?", (agent_dir.name,)))
    for row in connection.execute(
        "SELECT entry_key, subject_id, facts_json FROM archive_entries "
        "WHERE kind = ? AND state = ? ORDER BY entry_key",
        (ARCHIVE_KIND_AGENT, ARCHIVE_STATE_ARCHIVED),
    ):
        project_id = _root_project(_facts(str(row["facts_json"])))
        if project_id is not None:
            rooted.append(
                (
                    str(row["subject_id"]),
                    project_id,
                    "session_key IN (SELECT session_key FROM archive_entry_sessions "
                    "WHERE entry_key = ?)",
                    (int(row["entry_key"]),),
                )
            )
    return rooted


def _root_project(document: dict[str, Any] | None) -> str | None:
    value = None if document is None else document.get("root_project_id")
    return value if isinstance(value, str) and value else None


def _project_exists(data_dir: Path, project_id: str) -> bool:
    """Whether a Project is stored under exactly ``project_id``, as the Project store reads it."""
    projects_dir = data_dir / "projects"
    try:
        return has_id_entry(projects_dir, project_id) and is_file_strict(
            projects_dir / project_id / "project.json"
        )
    except OSError:
        return False


def _document(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except OSError, ValueError:
        return None
    return value if isinstance(value, dict) else None


def _facts(payload: str) -> dict[str, Any] | None:
    try:
        value = json.loads(payload)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
