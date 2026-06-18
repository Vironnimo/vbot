"""core.projects — project entity, anchor lifecycle, and cwd handling.

Small public interface over a deep module: the :class:`Project` entity and its
errors, the :class:`ProjectStore` anchor CRUD, and the cwd-normalization helpers
that the rest of the system uses for duplicate detection and re-point.
"""

from core.projects.paths import (
    cwd_exists,
    cwd_identity_key,
    normalize_cwd,
    slugify_project_id,
)
from core.projects.projects import (
    InvalidProjectIdError,
    Project,
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
    build_project,
    project_from_dict,
)
from core.projects.store import ProjectStore

__all__ = [
    "InvalidProjectIdError",
    "Project",
    "ProjectAlreadyExistsError",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectStore",
    "build_project",
    "cwd_exists",
    "cwd_identity_key",
    "normalize_cwd",
    "project_from_dict",
    "slugify_project_id",
]
