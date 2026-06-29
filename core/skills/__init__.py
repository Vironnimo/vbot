"""core.skills — local skill metadata registry."""

from core.skills.authoring import (
    SkillAuthor,
    SkillAuthoringError,
    SkillAuthoringService,
    SkillWriteResult,
)
from core.skills.requirements import SkillAvailability, SkillRequirements
from core.skills.skills import (
    FRONT_MATTER_DELIMITER,
    SKILL_ORIGIN_AGENT,
    SKILL_ORIGIN_BUNDLED,
    SKILL_ORIGIN_GLOBAL,
    SKILL_ORIGIN_PROJECT_PREFIX,
    WILDCARD_ALLOWLIST,
    SkillMetadata,
    SkillRegistry,
    load_project_skill_registry,
    project_skill_origin,
    project_skills_dir,
    scan_project_skill_names,
    scan_skill_names,
    skill_origin_sort_key,
)

__all__ = [
    "FRONT_MATTER_DELIMITER",
    "SKILL_ORIGIN_AGENT",
    "SKILL_ORIGIN_BUNDLED",
    "SKILL_ORIGIN_GLOBAL",
    "SKILL_ORIGIN_PROJECT_PREFIX",
    "SkillAuthor",
    "SkillAuthoringError",
    "SkillAuthoringService",
    "SkillAvailability",
    "SkillMetadata",
    "SkillRegistry",
    "SkillRequirements",
    "SkillWriteResult",
    "WILDCARD_ALLOWLIST",
    "load_project_skill_registry",
    "project_skill_origin",
    "project_skills_dir",
    "scan_project_skill_names",
    "scan_skill_names",
    "skill_origin_sort_key",
]
