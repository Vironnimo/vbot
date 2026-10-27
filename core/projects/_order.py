"""The user's Project order: ``projects/order.json``, shared by every accessor.

The document lists Project ids in the order the user arranged them and carries a
revision that a reorder must name, so a stale view cannot overwrite an order it
never saw. Reads never write it: Projects missing from it follow in id order,
ids of Projects no longer registered are skipped, and the next reorder stores
exactly the current Projects. An archived Project restored under its id
therefore returns to its old place until a reorder drops it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    strip_unknown_fields,
    validate_format_version,
    write_json_document,
)
from core.projects.projects import Project, ProjectError
from core.settings import is_valid_project_id
from core.utils.user_order import validate_order_fields

PROJECT_ORDER_FILENAME = "order.json"
PROJECT_ORDER_FORMAT_VERSION = 1

_PROJECT_ORDER_FIELDS = frozenset({"project_ids", "revision"})
PROJECT_ORDER_SHAPE = json_document(_PROJECT_ORDER_FIELDS)


class InvalidProjectOrderError(ProjectError):
    """Raised when a requested Project order is malformed."""


class ProjectOrderConflictError(ProjectError):
    """Raised when a reorder was based on a stale Project list or revision."""

    def __init__(self, message: str, *, current_revision: int) -> None:
        super().__init__(message)
        self.current_revision = current_revision


@dataclass(frozen=True)
class ProjectListResult:
    """The Projects in the user's order and the revision of that order."""

    projects: tuple[Project, ...]
    order_revision: int
    order_changed: bool = False


@dataclass(frozen=True)
class ProjectOrder:
    project_ids: tuple[str, ...]
    revision: int


def validate_project_order_file(order_path: str | Path) -> JsonValidationReport:
    """Validate the optional persisted Project order document."""
    return validate_json_file(order_path, validate_project_order_data, missing_ok=True)


def validate_project_order_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``projects/order.json`` document."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    if not validate_format_version(diagnostics, data, PROJECT_ORDER_FORMAT_VERSION):
        return diagnostics

    warn_unknown_keys(diagnostics, "$", data, PROJECT_ORDER_SHAPE.fields, "project order field")
    validate_order_fields(
        diagnostics,
        data,
        ids_field="project_ids",
        valid_id=is_valid_project_id,
        item_label="Project",
    )
    return diagnostics


PROJECT_ORDER_FORMAT = JsonDocumentFormat(
    name="Project order",
    version=PROJECT_ORDER_FORMAT_VERSION,
    shape=PROJECT_ORDER_SHAPE,
    validate=validate_project_order_data,
)


def load_project_order(order_path: Path) -> ProjectOrder | None:
    """Return the stored order, ``None`` when there is none yet.

    An invalid document raises ``JsonConfigValidationError``.
    """
    data = load_validated_json_file(
        order_path, validate_project_order_data, missing_ok=True, missing_default=None
    )
    if data is None:
        return None
    data = strip_unknown_fields(data, PROJECT_ORDER_SHAPE)
    return ProjectOrder(project_ids=tuple(data["project_ids"]), revision=data["revision"])


def write_project_order(order_path: Path, order: ProjectOrder) -> None:
    write_json_document(
        order_path,
        {"revision": order.revision, "project_ids": list(order.project_ids)},
        PROJECT_ORDER_FORMAT,
    )
