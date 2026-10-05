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
    PROJECT_FORMAT_VERSION,
    InvalidProjectIdError,
    Project,
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
    build_project,
    load_validated_project_json,
    project_from_dict,
    project_tool_configurability_reason,
    validate_project_data,
    validate_project_file,
)
from core.projects.resolver import (
    AGENT_OVERRIDE_FIELDS,
    AgentOverrides,
    AgentResolutionError,
    AgentResolver,
    ConfigAgent,
    ModelConfigurationChecker,
    ModelConfigurationError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    RuntimeAgent,
    WorkingProjectMissingError,
    build_agent_resolver,
    effective_project_allowed_skills,
    resolve_prompt_project,
    resolve_skill_scope,
    runtime_agent_body,
)
from core.projects.store import ArchivedProjectPayload, ProjectStore

__all__ = [
    "AGENT_OVERRIDE_FIELDS",
    "PROJECT_FORMAT_VERSION",
    "AgentOverrides",
    "AgentResolutionError",
    "AgentResolver",
    "ArchivedProjectPayload",
    "ConfigAgent",
    "InvalidAgentAddressError",
    "InvalidProjectIdError",
    "ModelConfigurationError",
    "ModelConfigurationChecker",
    "Project",
    "ProjectAlreadyExistsError",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectStore",
    "ResolutionAgentNotFoundError",
    "ResolutionProjectNotFoundError",
    "RuntimeAgent",
    "WorkingProjectMissingError",
    "build_agent_resolver",
    "build_project",
    "cwd_exists",
    "cwd_identity_key",
    "effective_project_allowed_skills",
    "format_agent_address",
    "load_validated_project_json",
    "normalize_cwd",
    "parse_agent_address",
    "project_from_dict",
    "project_tool_configurability_reason",
    "resolve_prompt_project",
    "resolve_skill_scope",
    "runtime_agent_body",
    "slugify_agent_id",
    "slugify_project_id",
    "validate_project_data",
    "validate_project_file",
]
