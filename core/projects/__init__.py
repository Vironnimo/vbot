"""core.projects — project entity, anchor lifecycle, and cwd handling.

Small public interface over a deep module: the :class:`Project` entity and its
errors, the :class:`ProjectStore` anchor CRUD, and the cwd-normalization helpers
that the rest of the system uses for duplicate detection and re-point.
"""

from core.projects.address import (
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.projects.paths import (
    cwd_exists,
    cwd_identity_key,
    normalize_cwd,
    slugify_agent_id,
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
from core.projects.resolver import (
    AgentResolutionError,
    AgentResolver,
    ConfigAgent,
    ModelConfigurationChecker,
    RuntimeAgent,
    build_agent_resolver,
    resolve_prompt_project,
    runtime_agent_body,
)
from core.projects.store import ProjectStore, project_sessions_dir

__all__ = [
    "AgentResolutionError",
    "AgentResolver",
    "ConfigAgent",
    "InvalidAgentAddressError",
    "InvalidProjectIdError",
    "ModelConfigurationChecker",
    "Project",
    "ProjectAlreadyExistsError",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectStore",
    "RuntimeAgent",
    "build_agent_resolver",
    "build_project",
    "cwd_exists",
    "cwd_identity_key",
    "format_agent_address",
    "normalize_cwd",
    "parse_agent_address",
    "project_from_dict",
    "project_sessions_dir",
    "resolve_prompt_project",
    "runtime_agent_body",
    "slugify_agent_id",
    "slugify_project_id",
]
