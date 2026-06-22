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

import builtins
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
    seed_default_auto_load,
)
from core.settings import SettingsValidationError, load_validated_project_json
from core.utils.logging import get_logger

_LOGGER = get_logger("projects")

_PROJECT_CONFIG_FILENAME = "project.json"
_PROJECTS_DIRNAME = "projects"
_AGENTS_DIRNAME = "agents"
_SESSIONS_DIRNAME = "sessions"
_WORKSPACE_DIRNAME = "workspace"

# Session-file recognition for anchor ownership enumeration. Kept local rather
# than imported from ``core.sessions`` because that module imports this one
# (``project_sessions_dir``); duplicating one extension literal avoids the cycle.
_SESSION_FILE_GLOB = "*.jsonl"


def project_sessions_dir(data_dir: Path, project_id: str, agent_id: str) -> Path:
    """Return the project-scoped sessions directory for one agent.

    ``<data_dir>/projects/<project-id>/agents/<agent-id>/sessions/``. This is the
    single source of the project-anchor session layout: both
    :meth:`ProjectStore.sessions_dir` and the session backbone
    (:class:`core.sessions.ChatSessionManager`) resolve project-scoped session
    paths through here, so the layout literal lives in exactly one place.
    """
    return (
        data_dir / _PROJECTS_DIRNAME / project_id / _AGENTS_DIRNAME / agent_id / _SESSIONS_DIRNAME
    )


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
        default_temperature: float | None = None,
        default_thinking_effort: str | None = None,
        auto_load: list[str] | None = None,
    ) -> Project:
        """Create and persist a project anchor and its ``agents/`` subtree.

        Rejects a duplicate id and a cwd already claimed by another project
        (same folder twice is not a valid case). The cwd folder itself does not
        have to exist yet — a bare/missing repo is detected at open time, not
        here — but it is normalized (symlinks, ``.``/``..``) before storage.
        """
        # A new project starts with AGENTS.md seeded as its first auto-load entry
        # (the project-instruction convention). Seeded here, in create only — never
        # in build_project, which update shares — so removing it later sticks.
        project = build_project(
            project_id,
            display_name,
            cwd,
            default_agent=default_agent,
            default_model=default_model,
            default_temperature=default_temperature,
            default_thinking_effort=default_thinking_effort,
            auto_load=seed_default_auto_load(auto_load),
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
            "default_temperature",
            "default_thinking_effort",
            "auto_load",
            "allowed_tools",
            "skills_bundled_enabled",
            "skills_project_disabled",
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
            default_temperature=changes.get("default_temperature", project.default_temperature),
            default_thinking_effort=changes.get(
                "default_thinking_effort", project.default_thinking_effort
            ),
            auto_load=changes.get("auto_load", list(project.auto_load)),
            allowed_tools=changes.get("allowed_tools", list(project.allowed_tools)),
            skills_bundled_enabled=changes.get(
                "skills_bundled_enabled", list(project.skills_bundled_enabled)
            ),
            skills_project_disabled=changes.get(
                "skills_project_disabled", list(project.skills_project_disabled)
            ),
            # model_overrides is not a generic update field (it has its own atomic
            # set/clear seam below); always carry the current map through so an
            # unrelated edit never drops a pinned override.
            model_overrides=dict(project.model_overrides),
            created_at=project.created_at,
        )

        if "cwd" in changes and rebuilt.cwd != project.cwd:
            self._reject_duplicate_cwd(rebuilt.cwd, exclude_project_id=project_id)

        updated = replace(rebuilt, updated_at=_utc_now())
        self._write_project(updated)
        return updated

    def set_model_override(self, project_id: str, agent_id: str, model: str) -> Project:
        """Pin a per-agent model override, replacing any existing entry for that agent.

        Atomic read-modify-write over ``project.json``: load the project, copy its
        override map, set ``agent_id → model``, and rewrite — leaving every other
        entry and field intact. The id/model shape is validated through
        ``build_project``; whether the model is *configured in this instance* is the
        caller's gate (the ``/model`` command path), not enforced here. Returns the
        updated project.
        """
        project = self.get(project_id)
        overrides = dict(project.model_overrides)
        overrides[agent_id] = model
        return self._rewrite_with_model_overrides(project, overrides)

    def clear_model_override(self, project_id: str, agent_id: str) -> Project:
        """Remove one agent's model override; clearing an absent entry is a no-op success.

        The config agent then falls back to its repo-declared model (or the
        project/global default). When the agent has no override, the project is
        returned unchanged without a write; otherwise exactly that one entry is
        dropped and the rest of the map is preserved.
        """
        project = self.get(project_id)
        if agent_id not in project.model_overrides:
            return project
        overrides = dict(project.model_overrides)
        del overrides[agent_id]
        return self._rewrite_with_model_overrides(project, overrides)

    def _rewrite_with_model_overrides(self, project: Project, overrides: dict[str, str]) -> Project:
        """Rebuild a project with a new override map and persist it atomically.

        Carries every other field unchanged through ``build_project`` (the single
        validation path), refreshes ``updated_at``, and writes via the atomic
        replace. The cwd re-normalization is idempotent on an already-stored project,
        exactly as in :meth:`update`.
        """
        rebuilt = build_project(
            project.project_id,
            project.display_name,
            project.cwd,
            default_agent=project.default_agent,
            default_model=project.default_model,
            default_temperature=project.default_temperature,
            default_thinking_effort=project.default_thinking_effort,
            auto_load=list(project.auto_load),
            allowed_tools=list(project.allowed_tools),
            skills_bundled_enabled=list(project.skills_bundled_enabled),
            skills_project_disabled=list(project.skills_project_disabled),
            model_overrides=overrides,
            created_at=project.created_at,
        )
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
        on-disk path the project-scoped session backbone uses in place of the
        global ``agents/<id>/sessions/``; the layout lives in
        :func:`project_sessions_dir` so the session manager shares it.
        """
        return project_sessions_dir(self._data_dir, project_id, agent_id)

    def session_owning_agents(self, project_id: str) -> builtins.list[str]:
        """Return the agent ids that own at least one session under this anchor.

        Walks ``projects/<project-id>/agents/<agent-id>/sessions/`` and keeps an
        agent only when its sessions directory actually holds a session file.
        This is the single enumeration point for project-scoped session
        discovery (statistics, recall): it reflects what the anchor owns *now*,
        so an agent dir created without sessions, or one whose sessions were all
        removed, does not appear. Returns ids sorted for determinism; an unknown
        project yields an empty list rather than raising.
        """
        agents_dir = self._project_dir(project_id) / _AGENTS_DIRNAME
        if not agents_dir.exists():
            return []
        owners = [
            agent_dir.name
            for agent_dir in agents_dir.iterdir()
            if agent_dir.is_dir() and _has_session_file(agent_dir / _SESSIONS_DIRNAME)
        ]
        return sorted(owners)

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


def _has_session_file(sessions_dir: Path) -> bool:
    """Return whether a sessions directory holds at least one session file."""
    if not sessions_dir.is_dir():
        return False
    return any(sessions_dir.glob(_SESSION_FILE_GLOB))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
