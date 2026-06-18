"""Project anchor lifecycle: data-dir layout, CRUD, and archive-on-remove.

The anchor is the runtime home of a project in the **data-dir** — never in the
repo (see add-projects.md → Speicherort & Datenmodell). Layout::

    <data_dir>/projects/<project-id>/
        project.json                 ← cwd, default agent/model, auto_load
        agents/<agent-id>/
            sessions/                ← project-scoped session ownership
            workspace/               ← only for a rooted identity agent

The anchor holds **no run config** — only Sessions ownership and the local
agent id; config comes live from the scan/repo (decision #4). This module owns
creation, read, list, cwd-mutation, and removal. Removal **archives** the
project subtree using the same mechanic as agent deletion
(``shutil.move`` into ``<data_dir>/archive/...`` replacing an existing archive),
so nothing is hard-deleted and the repo is never touched.

The duplicate-cwd guard lives here: two projects may not point at the same repo
(compared via :func:`core.projects.paths.cwd_identity_key`).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.projects.paths import cwd_identity_key
from core.projects.projects import (
    Project,
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
    build_project,
    project_from_dict,
)
from core.settings import SettingsValidationError, load_validated_project_json
from core.utils.logging import get_logger

_LOGGER = get_logger("projects")

_PROJECT_CONFIG_FILENAME = "project.json"
_PROJECTS_DIRNAME = "projects"
_AGENTS_DIRNAME = "agents"
_SESSIONS_DIRNAME = "sessions"
_WORKSPACE_DIRNAME = "workspace"
# Project archives live under their own subtree so a project id can never
# collide with an agent id in the shared archive namespace.
_ARCHIVE_PROJECTS_DIRNAME = "projects"
_ARCHIVE_DIRNAME = "archive"


class ProjectStore:
    """CRUD store for project anchors rooted at a data directory."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).expanduser()

    @property
    def data_dir(self) -> Path:
        """Root directory containing project anchors and archives."""
        return self._data_dir

    def create(
        self,
        project_id: str,
        display_name: str,
        cwd: str | os.PathLike[str],
        *,
        default_agent: str = "",
        default_model: str = "",
        auto_load: list[str] | None = None,
    ) -> Project:
        """Create and persist a project anchor and its ``agents/`` subtree.

        Rejects a duplicate id and a cwd already claimed by another project
        (same folder twice is not a valid case). The cwd folder itself does not
        have to exist yet — a bare/missing repo is detected at open time, not
        here — but it is normalized (symlinks, ``.``/``..``) before storage.
        """
        project = build_project(
            project_id,
            display_name,
            cwd,
            default_agent=default_agent,
            default_model=default_model,
            auto_load=auto_load,
        )

        project_dir = self._project_dir(project.project_id)
        if project_dir.exists():
            raise ProjectAlreadyExistsError(f"Project already exists: {project.project_id}")

        self._reject_duplicate_cwd(project.cwd, exclude_project_id=None)

        agents_dir = project_dir / _AGENTS_DIRNAME
        agents_dir.mkdir(parents=True)
        self._write_project(project)
        return project

    def get(self, project_id: str) -> Project:
        """Load one project anchor by id."""
        config_path = self._config_path(project_id)
        if not config_path.exists():
            raise ProjectNotFoundError(f"Project not found: {project_id}")
        return self._read_project(config_path)

    def exists(self, project_id: str) -> bool:
        """Return whether a project anchor with this id exists."""
        return self._config_path(project_id).exists()

    def list(self) -> list[Project]:
        """Return all persisted projects sorted by id.

        A single corrupt ``project.json`` is skipped with a logged warning
        rather than aborting the whole listing; strict access stays in
        :meth:`get`.
        """
        projects_dir = self._data_dir / _PROJECTS_DIRNAME
        if not projects_dir.exists():
            return []

        projects: list[Project] = []
        for config_path in sorted(projects_dir.glob(f"*/{_PROJECT_CONFIG_FILENAME}")):
            try:
                projects.append(self._read_project(config_path))
            except ProjectError as error:
                _LOGGER.warning("Skipping invalid project config %s: %s", config_path, error)
        return sorted(projects, key=lambda project: project.project_id)

    def update(self, project_id: str, **changes: Any) -> Project:
        """Update mutable project fields. ``project_id`` is immutable.

        Changing ``cwd`` re-normalizes the path and re-checks the duplicate-cwd
        guard against every other project. ``project_id`` is immutable: it is not
        an updatable field, so passing it (the anchor directory name) is rejected
        as an unknown field rather than silently moving the anchor.
        """
        project = self.get(project_id)
        if not changes:
            return project

        allowed_fields = {
            "display_name",
            "cwd",
            "default_agent",
            "default_model",
            "auto_load",
        }
        unknown_fields = sorted(set(changes) - allowed_fields)
        if unknown_fields:
            raise ProjectError(f"Unknown project fields: {', '.join(unknown_fields)}")

        # Re-run the field validation by rebuilding through ``build_project``,
        # carrying immutable identity/timestamps; this keeps one validation path.
        rebuilt = build_project(
            project.project_id,
            changes.get("display_name", project.display_name),
            changes.get("cwd", project.cwd),
            default_agent=changes.get("default_agent", project.default_agent),
            default_model=changes.get("default_model", project.default_model),
            auto_load=changes.get("auto_load", list(project.auto_load)),
            created_at=project.created_at,
        )

        if "cwd" in changes and rebuilt.cwd != project.cwd:
            self._reject_duplicate_cwd(rebuilt.cwd, exclude_project_id=project_id)

        updated = replace(rebuilt, updated_at=_utc_now())
        self._write_project(updated)
        return updated

    def delete(self, project_id: str) -> Path:
        """Archive the project anchor subtree and remove the active copy.

        Mirrors :meth:`core.agents.AgentStore.delete`: move the active directory
        under ``<data_dir>/archive/projects/<project-id>/``, replacing an
        existing archive for the same id. The repo (cwd) is never touched —
        removing a project is not deleting a repo. Returns the archive path.
        """
        project_dir = self._project_dir(project_id)
        if not project_dir.exists():
            raise ProjectNotFoundError(f"Project not found: {project_id}")

        archive_dir = self._archive_dir(project_id)
        if archive_dir.exists():
            shutil.rmtree(archive_dir)
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(project_dir), str(archive_dir))
        return archive_dir

    def sessions_dir(self, project_id: str, agent_id: str) -> Path:
        """Return the project-scoped sessions directory for one agent.

        ``projects/<project-id>/agents/<agent-id>/sessions/``. This is the
        on-disk path that the project-scoped session backbone (Phase 2) will use
        in place of the global ``agents/<id>/sessions/``.
        """
        return self._agent_anchor_dir(project_id, agent_id) / _SESSIONS_DIRNAME

    def workspace_dir(self, project_id: str, agent_id: str) -> Path:
        """Return the rooted-identity-agent workspace dir under the anchor.

        ``projects/<project-id>/agents/<agent-id>/workspace/`` — only a rooted
        identity agent populates this; config agents never have a workspace.
        """
        return self._agent_anchor_dir(project_id, agent_id) / _WORKSPACE_DIRNAME

    def _agent_anchor_dir(self, project_id: str, agent_id: str) -> Path:
        return self._project_dir(project_id) / _AGENTS_DIRNAME / agent_id

    def _project_dir(self, project_id: str) -> Path:
        return self._data_dir / _PROJECTS_DIRNAME / project_id

    def _config_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / _PROJECT_CONFIG_FILENAME

    def _archive_dir(self, project_id: str) -> Path:
        return self._data_dir / _ARCHIVE_DIRNAME / _ARCHIVE_PROJECTS_DIRNAME / project_id

    def _reject_duplicate_cwd(self, cwd: str, *, exclude_project_id: str | None) -> None:
        target_key = cwd_identity_key(cwd)
        for existing in self.list():
            if existing.project_id == exclude_project_id:
                continue
            if cwd_identity_key(existing.cwd) == target_key:
                raise ProjectAlreadyExistsError(
                    f"A project already points at this folder: {existing.project_id}"
                )

    def _write_project(self, project: Project) -> None:
        config_path = self._config_path(project.project_id)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = config_path.with_name(f".{config_path.name}.tmp")
        temp_path.write_text(
            json.dumps(project.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, config_path)

    def _read_project(self, config_path: Path) -> Project:
        try:
            data = load_validated_project_json(config_path)
        except SettingsValidationError as error:
            raise ProjectError(str(error)) from error

        project = project_from_dict(data)
        if project.project_id != config_path.parent.name:
            raise ProjectError(
                f"Project id mismatch for {config_path}: "
                f"expected {config_path.parent.name}, got {project.project_id}"
            )
        return project


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
