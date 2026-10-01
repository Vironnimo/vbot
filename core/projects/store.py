"""Project anchor lifecycle: data-dir layout, CRUD, and archive-on-remove.

The anchor is the runtime home of a project in the **data-dir** — never in the
repo (see add-projects.md → Speicherort & Datenmodell). Layout::

    <data_dir>/projects/<project-id>/
        project.json                 ← cwd, default agent/model, auto_load

The anchor holds **no run config** — only Project configuration; config comes live
from the scan/repo. This module owns creation, read, list, cwd-mutation, and
moving the Anchor into an archive payload and back (``archive_files`` and
``restore_files``); the archive domain (``core.archive``) decides where payloads
live and records them. The repo is never touched.

The duplicate-cwd guard lives here: two projects may not point at the same repo
(compared via :func:`core.projects.paths.cwd_identity_key`).
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from core.database import SnapshotBarrier
from core.json_documents import (
    JsonDocumentWriteError,
    document_change,
    document_version_state,
    write_json_document,
)
from core.projects.paths import cwd_identity_key
from core.projects.projects import (
    PROJECT_FORMAT_VERSION,
    Project,
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
    build_project,
    load_validated_project_json,
    project_format,
    project_from_dict,
    seed_default_auto_load,
)
from core.settings import (
    DEFAULT_PROJECT_SOURCE_FORMAT,
    is_valid_project_id,
)
from core.utils.atomic import atomic_write_bytes
from core.utils.ids import has_id_entry
from core.utils.logging import get_logger
from core.utils.tree_move import move_tree

if TYPE_CHECKING:
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("projects")

_PROJECT_CONFIG_FILENAME = "project.json"
_PROJECTS_DIRNAME = "projects"


def _validate_project_id(project_id: str) -> str:
    """Reject any project id that is not a bare slug before it becomes a path segment.

    The id is a path segment under ``<data_dir>/projects/`` and
    :meth:`ProjectStore.archive_files` moves that directory with ``move_tree``. A
    separator or ``..`` component (``../agents``, ``/etc``) would let an operation
    escape the projects subtree and move an arbitrary directory.
    Every legitimately created id is a :func:`slugify_project_id` slug, so this only ever
    rejects crafted input — and it does so at the path-building choke point every store
    and session-path call funnels through, not only at config validation.
    """
    if not is_valid_project_id(project_id):
        raise ProjectError(f"Invalid project id: {project_id!r}")
    return project_id


@dataclass(frozen=True)
class ArchivedProjectPayload:
    """An archived Anchor as read from its payload: the Project, or why it cannot return.

    ``problem`` is ``payload_missing``, ``payload_invalid``, ``older_format`` or
    ``newer_format``.
    """

    project: Project | None
    problem: str | None = None


class ProjectStore:
    """CRUD store for project anchors rooted at a data directory."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        sessions: ChatSessionManager | None = None,
        snapshot_barrier: SnapshotBarrier | None = None,
    ) -> None:
        self._data_dir = Path(data_dir).expanduser()
        self._sessions = sessions
        self._owns_sessions = sessions is None
        # Commands and RPC workers share this store. Serialize complete config
        # transactions, including duplicate-cwd checks and failure compensation.
        # Event Loop code takes it for reads, so no thread may wait for a data
        # snapshot while holding it: a mutation admits its document change first
        # (``_change``).
        self._write_lock = RLock()
        # Deletion archives the anchor and its Sessions together; a data snapshot
        # of the Runtime's barrier never copies between the two.
        self._snapshot_barrier = (
            snapshot_barrier if snapshot_barrier is not None else SnapshotBarrier()
        )

    def close(self) -> None:
        with self._write_lock:
            if self._owns_sessions and self._sessions is not None:
                self._sessions.close()
                self._sessions = None
                self._owns_sessions = False

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
        source_format: str = DEFAULT_PROJECT_SOURCE_FORMAT,
        auto_load: list[str] | None = None,
    ) -> Project:
        """Create and persist a project anchor.

        Rejects a duplicate id and a cwd already claimed by another project
        (same folder twice is not a valid case). The cwd folder itself does not
        have to exist yet — a bare/missing repo is detected at open time, not
        here — but it is normalized (symlinks, ``.``/``..``) before storage.
        """
        # A new project starts with AGENTS.md seeded as its first auto-load entry
        # (the project-instruction convention). Seeded here, in create only — never
        # in build_project, which update shares — so removing it later sticks.
        with self._change():
            project = build_project(
                project_id,
                display_name,
                cwd,
                default_agent=default_agent,
                default_model=default_model,
                default_temperature=default_temperature,
                default_thinking_effort=default_thinking_effort,
                source_format=source_format,
                auto_load=seed_default_auto_load(auto_load),
            )

            project_dir = self._project_dir(project.project_id)
            if project_dir.exists():
                raise ProjectAlreadyExistsError(f"Project already exists: {project.project_id}")

            self._reject_duplicate_cwd(project.cwd, exclude_project_id=None)

            project_dir.mkdir(parents=True)
            try:
                self._write_project(project)
            except Exception:
                with document_change(project_dir):
                    shutil.rmtree(project_dir)
                raise
            return project

    def get(self, project_id: str) -> Project:
        """Load one project anchor by its exact id."""
        with self._write_lock:
            config_path = self._stored_config_path(project_id)
            if config_path is None:
                raise ProjectNotFoundError(f"Project not found: {project_id}")
            return self._read_project(config_path)

    def exists(self, project_id: str) -> bool:
        """Return whether a valid Project with exactly this id can be loaded."""
        with self._write_lock:
            try:
                config_path = self._stored_config_path(project_id)
                if config_path is None:
                    return False
                self._read_project(config_path)
            except (ProjectError, OSError):
                return False
            return True

    def list(self) -> list[Project]:
        """Return all persisted projects sorted by id.

        A single corrupt ``project.json`` is skipped with a logged warning
        rather than aborting the whole listing; strict access stays in
        :meth:`get`.
        """
        with self._write_lock:
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

    def find_by_cwd(self, cwd: str | os.PathLike[str]) -> Project | None:
        """Return the project whose repo cwd is ``cwd``, or ``None`` if none match.

        Equality is decided by :func:`core.projects.paths.cwd_identity_key` — the
        same realpath + Windows-only case-fold rule the duplicate-cwd guard uses —
        so a path that resolves to a registered project's repo matches it
        regardless of symlinks or path-case differences. cwd is unique across
        projects (the duplicate-cwd guard), so at most one project matches. Used to
        discover which registered Project owns an arbitrary repository path; an
        empty or unresolvable path yields ``None`` rather than raising.
        """
        with self._write_lock:
            try:
                target_key = cwd_identity_key(cwd)
            except ValueError:
                return None
            for project in self.list():
                if cwd_identity_key(project.cwd) == target_key:
                    return project
            return None

    def update(self, project_id: str, **changes: Any) -> Project:
        """Update mutable project fields. ``project_id`` is immutable.

        Changing ``cwd`` re-normalizes the path and re-checks the duplicate-cwd
        guard against every other project. ``project_id`` is immutable: it is not
        an updatable field, so passing it (the anchor directory name) is rejected
        as an unknown field rather than silently moving the anchor.
        """
        with self._change():
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
                "source_format",
                "auto_load",
                "allowed_tools",
                "skills_bundled_enabled",
                "skills_global_enabled",
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
                source_format=changes.get("source_format", project.source_format),
                auto_load=changes.get("auto_load", list(project.auto_load)),
                allowed_tools=changes.get("allowed_tools", list(project.allowed_tools)),
                skills_bundled_enabled=changes.get(
                    "skills_bundled_enabled", list(project.skills_bundled_enabled)
                ),
                skills_global_enabled=changes.get(
                    "skills_global_enabled", list(project.skills_global_enabled)
                ),
                skills_project_disabled=changes.get(
                    "skills_project_disabled", list(project.skills_project_disabled)
                ),
                # overrides is not a generic update field (it has its own atomic per-field
                # set/clear seam below); always carry the current map through so an
                # unrelated edit never drops an override.
                overrides=_copy_overrides(project.overrides),
                created_at=project.created_at,
            )

            if "cwd" in changes and rebuilt.cwd != project.cwd:
                self._reject_duplicate_cwd(rebuilt.cwd, exclude_project_id=project_id)

            updated = replace(rebuilt, updated_at=_utc_now())
            self._write_project(updated)
            return updated

    def set_override(self, project_id: str, agent_id: str, field: str, value: Any) -> Project:
        """Override one field (``model`` / ``temperature`` / ``thinking_effort``) for an agent.

        Atomic read-modify-write over ``project.json``: load the project, copy its
        override map, set ``agent_id`` → ``{…, field: value}`` (merging into any
        existing override for that agent), and rewrite — leaving every other agent and
        every other field intact. The field/value shape is validated through
        ``build_project`` (ranges/levels reuse the canonical agent validators); whether
        an overridden model is *configured in this instance* is the caller's gate (the
        ``/model`` command path), not enforced here. Returns the updated project.
        """
        with self._change():
            project = self.get(project_id)
            overrides = _copy_overrides(project.overrides)
            agent_override = dict(overrides.get(agent_id, {}))
            agent_override[field] = value
            overrides[agent_id] = agent_override
            return self._rewrite_with_overrides(project, overrides)

    def clear_override(self, project_id: str, agent_id: str, field: str) -> Project:
        """Remove one overridden field for an agent; clearing an absent field is a no-op success.

        The config agent's matching chain then falls back to its repo-declared value
        (or the project/global default). Clearing the agent's **last** overridden field
        removes the agent's entry from ``project.json`` unless it still holds fields this
        vBot does not model, which stay on disk. When the agent has no such overridden
        field, the project is returned unchanged without a write; otherwise exactly that
        one field is dropped and every other override and field is preserved.
        """
        with self._change():
            project = self.get(project_id)
            agent_override = project.overrides.get(agent_id)
            if agent_override is None or field not in agent_override:
                return project
            overrides = _copy_overrides(project.overrides)
            updated_override = dict(overrides[agent_id])
            del updated_override[field]
            # An empty entry keeps its unknown fields on write, or is left out.
            overrides[agent_id] = updated_override
            return self._rewrite_with_overrides(project, overrides)

    def _rewrite_with_overrides(
        self, project: Project, overrides: dict[str, dict[str, Any]]
    ) -> Project:
        """Rebuild a project with a new override map and persist it atomically.

        Carries every other field unchanged through ``build_project`` (the single
        validation path), refreshes ``updated_at``, and writes via the atomic
        replace. The cwd re-normalization is idempotent on an already-stored project,
        exactly as in :meth:`update`. Returns the project as persisted: an emptied
        override entry remains only while it holds fields this vBot does not model.
        """
        rebuilt = build_project(
            project.project_id,
            project.display_name,
            project.cwd,
            default_agent=project.default_agent,
            default_model=project.default_model,
            default_temperature=project.default_temperature,
            default_thinking_effort=project.default_thinking_effort,
            source_format=project.source_format,
            auto_load=list(project.auto_load),
            allowed_tools=list(project.allowed_tools),
            skills_bundled_enabled=list(project.skills_bundled_enabled),
            skills_global_enabled=list(project.skills_global_enabled),
            skills_project_disabled=list(project.skills_project_disabled),
            overrides=overrides,
            created_at=project.created_at,
        )
        updated = replace(rebuilt, updated_at=_utc_now())
        self._write_project(updated)
        return self._read_project(self._config_path(project.project_id))

    @contextmanager
    def archive_files(self, project_id: str, tree: Path) -> Iterator[Project | None]:
        """Move the Project Anchor to the payload ``tree`` for the caller's commit.

        Use as ``with store.archive_files(project_id, tree) as project:`` and
        commit the Sessions inside the body; a failing body moves the Anchor
        back. The store lock and a compound mutation of the snapshot barrier are
        held across the body. Yields the Project, or ``None`` when its config
        cannot be read (the Anchor is archived all the same). The repo (cwd) is
        never touched. Product-level reference and Run admission guards belong
        to the caller.
        """
        with self._snapshot_barrier.compound_mutation(), self._change():
            project_dir = self._stored_project_dir(project_id)
            if project_dir is None:
                raise ProjectNotFoundError(f"Project not found: {project_id}")
            try:
                project: Project | None = self._read_project(project_dir / _PROJECT_CONFIG_FILENAME)
            except (ProjectError, OSError):
                project = None
            try:
                tree.parent.mkdir(parents=True, exist_ok=True)
                move_tree(project_dir, tree)
            except OSError as exc:
                raise ProjectError(f"Project archival failed: {exc}") from exc
            try:
                yield project
            except BaseException as exc:
                _move_back(tree, project_dir, exc, "Project archival failed")
                raise

    def inspect_archived(self, source: Path) -> ArchivedProjectPayload:
        """Read an archived Anchor directory without changing it; report why it cannot return."""
        path = source / _PROJECT_CONFIG_FILENAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ArchivedProjectPayload(None, "payload_missing")
        except (OSError, ValueError):
            return ArchivedProjectPayload(None, "payload_invalid")
        version = document_version_state(raw, PROJECT_FORMAT_VERSION)
        if version != "current":
            return ArchivedProjectPayload(None, f"{version}_format")
        try:
            return ArchivedProjectPayload(project_from_dict(load_validated_project_json(path)))
        except (ProjectError, OSError, ValueError):
            return ArchivedProjectPayload(None, "payload_invalid")

    def restore_target_problem(self, project_id: str) -> str | None:
        """Why an archived Anchor cannot return as ``project_id``: ``invalid_target_id``,
        ``project_id_taken``, or ``None`` when it can."""
        if not is_valid_project_id(project_id):
            return "invalid_target_id"
        projects_dir = self._data_dir / _PROJECTS_DIRNAME
        if has_id_entry(projects_dir, project_id) or os.path.lexists(projects_dir / project_id):
            return "project_id_taken"
        return None

    @contextmanager
    def restore_files(self, source: Path, target_id: str) -> Iterator[Project]:
        """Move an archived Anchor back as ``target_id`` for the caller's commit.

        Refuses a taken id and a repo another Project claims before anything
        changes. Once moved, its ``project.json`` is rewritten to the target id;
        unknown fields stay. If the rewrite or the body raises, ``project.json``
        gets its archived bytes back and the Anchor moves back into ``source``,
        so a failed restore leaves the payload as it was.
        """
        with self._snapshot_barrier.compound_mutation(), self._change():
            _validate_project_id(target_id)
            if self.restore_target_problem(target_id) is not None:
                raise ProjectAlreadyExistsError(f"Project already exists: {target_id}")
            project = project_from_dict(
                load_validated_project_json(source / _PROJECT_CONFIG_FILENAME)
            )
            self._reject_duplicate_cwd(project.cwd, exclude_project_id=None)
            restored = replace(project, project_id=target_id, updated_at=_utc_now())
            project_dir = self._project_dir(target_id)
            config = project_dir / _PROJECT_CONFIG_FILENAME
            try:
                archived_document = (source / _PROJECT_CONFIG_FILENAME).read_bytes()
                project_dir.parent.mkdir(parents=True, exist_ok=True)
                move_tree(source, project_dir)
            except OSError as exc:
                raise ProjectError(f"Project restore failed: {exc}") from exc
            try:
                try:
                    write_json_document(config, restored.to_dict(), project_format())
                except JsonDocumentWriteError as error:
                    raise ProjectError(str(error)) from error
                yield self._read_project(config)
            except BaseException as exc:
                try:
                    atomic_write_bytes(config, archived_document)
                except OSError as error:
                    # The payload keeps the rewritten document, which restores all the same.
                    _LOGGER.warning(
                        "Could not return %s to its archived content: %s", config, error
                    )
                _move_back(project_dir, source, exc, "Project restore failed")
                raise

    def session_owning_agents(self, project_id: str) -> builtins.list[str]:
        """Return the agent ids that own at least one session under this anchor.

        Queries canonical live Session addresses and keeps each distinct Agent.
        This is the single enumeration point for project-scoped Session
        discovery (Statistics, Team orphan findings). Extension-owned participant Sessions
        carry synthetic Agent ids outside the Team and are excluded; their owners
        report them separately. Returns ids sorted for determinism; an unknown
        Project yields an empty list rather than raising.
        """
        if self._stored_project_dir(project_id) is None:
            return []
        return self._session_manager().list_agent_ids(project_id, exclude_owner_managed=True)

    def _session_manager(self) -> ChatSessionManager:
        with self._write_lock:
            if self._sessions is None:
                from contextlib import suppress

                from core.sessions import ChatSessionManager
                from core.storage.layout import initialize_data_directory

                marker = self._data_dir / "data-store.json"
                if not marker.exists():
                    with suppress(Exception):
                        initialize_data_directory(self._data_dir)
                self._sessions = ChatSessionManager(self._data_dir)
                self._owns_sessions = True
            return self._sessions

    @contextmanager
    def _change(self) -> Iterator[None]:
        """Admit a change of the Project documents, then hold the store lock.

        In this order a change that waits for a data snapshot waits without the
        lock, which Event Loop readers take.
        """
        with document_change(self._data_dir / _PROJECTS_DIRNAME), self._write_lock:
            yield

    def _project_dir(self, project_id: str) -> Path:
        return self._data_dir / _PROJECTS_DIRNAME / _validate_project_id(project_id)

    def _config_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / _PROJECT_CONFIG_FILENAME

    def _stored_project_dir(self, project_id: str) -> Path | None:
        """Return the Project Anchor stored under exactly ``project_id``, or ``None``.

        Ids are exact on every platform. A case-insensitive filesystem would open
        the stored ``vbot`` Anchor for ``VBOT``; that different id names no Project.
        """
        project_dir = self._project_dir(project_id)
        if not has_id_entry(project_dir.parent, project_id) or not project_dir.is_dir():
            return None
        return project_dir

    def _stored_config_path(self, project_id: str) -> Path | None:
        """Return ``project.json`` of the Project stored under exactly ``project_id``."""
        project_dir = self._stored_project_dir(project_id)
        if project_dir is None:
            return None
        config_path = project_dir / _PROJECT_CONFIG_FILENAME
        return config_path if config_path.is_file() else None

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
        """Write one Anchor config inside an admitted change (``_change``); it never waits."""
        config_path = self._config_path(project.project_id)
        try:
            with document_change(config_path, wait=False):
                write_json_document(config_path, project.to_dict(), project_format())
        except JsonDocumentWriteError as error:
            raise ProjectError(str(error)) from error

    def _read_project(self, config_path: Path) -> Project:
        data = load_validated_project_json(config_path)
        project = project_from_dict(data)
        if project.project_id != config_path.parent.name:
            raise ProjectError(
                f"Project id mismatch for {config_path}: "
                f"expected {config_path.parent.name}, got {project.project_id}"
            )
        return project


def _move_back(source: Path, destination: Path, error: BaseException, failure: str) -> None:
    """Return a moved Anchor; when that fails too, say where the files are."""
    try:
        move_tree(source, destination)
    except OSError as move_error:
        raise ProjectError(
            f"{failure} ({error}); Project files retained at {source}"
        ) from move_error


def _copy_overrides(overrides: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return a deep-enough copy of an override map (each agent's override object copied)."""
    return {agent_id: dict(override) for agent_id, override in overrides.items()}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
