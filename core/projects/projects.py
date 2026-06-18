"""Project entity, ``project.json`` schema, and field validation.

This is the public/main file of the ``core/projects`` deep module. A Project is
a first-class entity (see GLOSSARY → Project): a stable ``project_id`` slug, a
changeable ``display_name``, the repo ``cwd`` that tools resolve relative paths
against, optional project-default agent/model pointers, and an ordered
``auto_load`` file list. The minimal valid Project is just a cwd — team,
AGENTS.md, and auto-load files are all optional.

Field rules are enforced once by ``core.settings.validate_project_data`` at load
time (the central validator), the same way Agents validate through the settings
domain. This module owns the entity shape and the create-time field validation;
the on-disk anchor lifecycle and CRUD live in ``core/projects/store.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from core.projects.paths import normalize_cwd
from core.settings import is_valid_project_id

DEFAULT_DEFAULT_AGENT = ""
DEFAULT_DEFAULT_MODEL = ""


class ProjectError(ValueError):
    """Base error for expected project lifecycle failures."""


class ProjectAlreadyExistsError(ProjectError):
    """Raised when creating a project whose id (or cwd) already exists."""


class ProjectNotFoundError(ProjectError):
    """Raised when a project cannot be found."""


class InvalidProjectIdError(ProjectError):
    """Raised when a project id is unsafe for filesystem use."""


@dataclass(frozen=True)
class Project:
    """Persisted project configuration stored in ``project.json``.

    The anchor directory name is the ``project_id``; the ``cwd`` lives in the
    file (not the directory name) so the repo folder can move without breaking
    the key or its Sessions.
    """

    project_id: str
    display_name: str
    cwd: str
    created_at: str
    updated_at: str
    default_agent: str = DEFAULT_DEFAULT_AGENT
    default_model: str = DEFAULT_DEFAULT_MODEL
    auto_load: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable mapping persisted to ``project.json``."""
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "cwd": self.cwd,
            "default_agent": self.default_agent,
            "default_model": self.default_model,
            "auto_load": list(self.auto_load),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def build_project(
    project_id: str,
    display_name: str,
    cwd: str | os.PathLike[str],
    *,
    default_agent: str = DEFAULT_DEFAULT_AGENT,
    default_model: str = DEFAULT_DEFAULT_MODEL,
    auto_load: list[str] | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> Project:
    """Validate fields and construct a :class:`Project` with normalized cwd.

    The ``cwd`` is resolved (symlinks, ``.``/``..``) and stored as an absolute
    path; case is preserved. Timestamps default to now (UTC ISO 8601 with
    offset). Raises :class:`ProjectError` / :class:`InvalidProjectIdError` on
    bad input.
    """
    validated_id = _validate_project_id(project_id)
    validated_display_name = _validate_non_empty_string("display_name", display_name)
    validated_cwd = str(_normalize_cwd(cwd))
    validated_default_agent = _validate_optional_string("default_agent", default_agent)
    validated_default_model = _validate_optional_string("default_model", default_model)
    validated_auto_load = _validate_auto_load(auto_load)
    now = _utc_now()
    return Project(
        project_id=validated_id,
        display_name=validated_display_name,
        cwd=validated_cwd,
        default_agent=validated_default_agent,
        default_model=validated_default_model,
        auto_load=validated_auto_load,
        created_at=created_at or now,
        updated_at=updated_at or now,
    )


def project_from_dict(data: dict[str, Any]) -> Project:
    """Build a Project from a mapping already validated by the central validator.

    ``core.settings.validate_project_data`` enforces the field rules at load
    time; this constructor only normalizes shapes (optional-field defaults,
    auto_load list copy), it does not re-validate.
    """
    return Project(
        project_id=data["project_id"],
        display_name=data["display_name"],
        cwd=data["cwd"],
        default_agent=data.get("default_agent", DEFAULT_DEFAULT_AGENT),
        default_model=data.get("default_model", DEFAULT_DEFAULT_MODEL),
        auto_load=list(cast("list[str]", data.get("auto_load") or [])),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _normalize_cwd(cwd: str | os.PathLike[str]) -> Any:
    try:
        return normalize_cwd(cwd)
    except ValueError as exc:
        raise ProjectError(f"cwd must be a non-empty path: {exc}") from exc


def _validate_project_id(project_id: Any) -> str:
    if not is_valid_project_id(project_id):
        raise InvalidProjectIdError(
            "Project id must be 1-64 characters using only letters, numbers, hyphen, or underscore"
        )
    return cast("str", project_id)


def _validate_non_empty_string(field_name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError(f"{field_name} must be a non-empty string")
    return value


def _validate_optional_string(field_name: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProjectError(f"{field_name} must be a string")
    return value


def _validate_auto_load(auto_load: list[str] | None) -> list[str]:
    if auto_load is None:
        return []
    if not isinstance(auto_load, list):
        raise ProjectError("auto_load must be a list of strings")
    for item in auto_load:
        if not isinstance(item, str) or not item.strip():
            raise ProjectError("auto_load entries must be non-empty strings")
    return list(auto_load)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
