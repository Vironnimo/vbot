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
    WILDCARD_ALLOWLIST,
    SkillMetadata,
    SkillRegistry,
    load_project_skill_registry,
    project_skills_dir,
    scan_project_skill_names,
    scan_skill_names,
)

__all__ = [
    "FRONT_MATTER_DELIMITER",
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
    "project_skills_dir",
    "scan_project_skill_names",
    "scan_skill_names",
]
