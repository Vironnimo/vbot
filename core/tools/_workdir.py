"""Project working directories for the Tools that start programs.

``powershell``/``bash`` and ``terminal`` accept ``workdir: "project:<project-id>"``
besides a path: the directory of that Project. Neither Tool advertises the form;
the Projects block teaches each Project's path. This module resolves it the same
way for both Tools; each Tool words the failure for its own result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from core.projects import ProjectError, ProjectNotFoundError, ProjectStore, cwd_exists
from core.utils.paths import model_path

PROJECT_WORKDIR_PREFIX = "project:"

ProjectWorkdirFailure = Literal["invalid_workdir", "project_not_found", "project_unavailable"]


class ProjectWorkdirError(ValueError):
    """A ``project:`` workdir that names no reachable Project directory.

    The message is a sentence for the Agent; ``code`` tells why.
    """

    def __init__(self, message: str, *, code: ProjectWorkdirFailure) -> None:
        super().__init__(message)
        self.code: ProjectWorkdirFailure = code


def is_project_workdir(value: str) -> bool:
    return value.startswith(PROJECT_WORKDIR_PREFIX)


def project_workdir(projects: ProjectStore | None, value: str) -> Path:
    """The directory of the Project a ``project:<project-id>`` workdir names.

    Raises ``ProjectWorkdirError`` when the id is empty, the Project does not
    exist or cannot be read, or its directory is missing; *projects* None means
    Projects are unavailable to the caller.
    """
    project_id = value.removeprefix(PROJECT_WORKDIR_PREFIX).strip()
    if not project_id:
        raise ProjectWorkdirError(
            f'workdir "{value}" names no Project. Pass a directory path, absolute or '
            "relative to the working directory.",
            code="invalid_workdir",
        )
    if projects is None:
        raise ProjectWorkdirError(
            f'workdir "{value}" names a Project, but Projects are unavailable here. Pass the '
            "Project's directory path instead.",
            code="project_unavailable",
        )
    try:
        project = projects.get(project_id)
    except ProjectNotFoundError as error:
        raise ProjectWorkdirError(
            f'workdir "{value}" names no existing Project. Pass the Project\'s directory '
            "path instead.",
            code="project_not_found",
        ) from error
    except (ProjectError, OSError) as error:
        raise ProjectWorkdirError(
            f"Project {project_id} could not be read: {error}",
            code="project_unavailable",
        ) from error
    if not cwd_exists(project.cwd):
        raise ProjectWorkdirError(
            f"the directory of Project {project_id}, {model_path(project.cwd)}, does not exist.",
            code="project_unavailable",
        )
    return Path(project.cwd).resolve()


__all__ = [
    "PROJECT_WORKDIR_PREFIX",
    "ProjectWorkdirError",
    "ProjectWorkdirFailure",
    "is_project_workdir",
    "project_workdir",
]
