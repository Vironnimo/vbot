"""Skill metadata registry for local agent skills.

Skills are reusable playbooks stored under ``<data_dir>/skills/<skill-id>/``.
Each skill directory must contain a ``SKILL.md`` file.  The registry reads the
Markdown front matter for prompt metadata and filters it through an agent's
``allowed_skills`` list.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.skills.requirements import (
    AVAILABLE,
    RequirementCheck,
    RequirementEvaluation,
    RequirementGroup,
    RequirementNode,
    RequirementParseError,
    SkillAvailability,
    SkillRequirements,
    parse_vbot_requirements,
)
from core.skills.skill_validator import (
    MALFORMED_YAML_FALLBACK_WARNING,
    ValidationResult,
    repair_colon_scalars,
    validate_skill_metadata,
)
from core.utils.logging import get_logger

FRONT_MATTER_DELIMITER = "---"
WILDCARD_ALLOWLIST = "*"
SKILL_FILENAME = "SKILL.md"
RESOURCE_DIRECTORIES = ("scripts", "references")
# A project's own skills live beside its OpenCode agents, under
# ``<repo>/.opencode/skills/`` (the v1 project-skill location). Scanned per project
# and merged with the bundled skills, project-first so a project skill wins a name
# collision with a bundled one (decision 3/4 in the whitelist plan).
PROJECT_SKILLS_SUBPATH = (".opencode", "skills")

_LOGGER = get_logger("skills")


@dataclass(frozen=True)
class SkillMetadata:
    """Metadata for a loadable local skill."""

    name: str
    description: str
    path: Path
    license: str | None = None
    compatibility: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    requirements: SkillRequirements = field(default_factory=SkillRequirements)


@dataclass(frozen=True)
class SkillDiagnostic:
    """Validation diagnostics for a loadable or rejected skill directory."""

    name: str
    path: Path
    valid: bool
    warnings: list[str]
    loadable: bool


class SkillRegistry:
    """Scans local skill directories and filters prompt-visible metadata."""

    def __init__(
        self,
        skills: dict[str, SkillMetadata],
        diagnostics: list[SkillDiagnostic] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._skills = skills
        self._diagnostics = list(diagnostics or [])
        self._environment = dict(os.environ if environment is None else environment)

    @classmethod
    def load(
        cls,
        skills_dir: Path,
        extra_dirs: list[Path] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> SkillRegistry:
        """Load all valid skills from immediate subdirectories of scan roots.

        Missing skill roots are treated as empty.  A directory is a skill only
        when it contains ``SKILL.md`` with loadable YAML front matter.  When
        duplicate names are found, the first scanned directory wins and the
        rejected duplicate is preserved as a diagnostic.
        """
        skills: dict[str, SkillMetadata] = {}
        diagnostics: list[SkillDiagnostic] = []
        scan_roots = [skills_dir, *(extra_dirs or [])]
        for scan_root in scan_roots:
            _load_skill_root(scan_root, skills, diagnostics)

        return cls(skills, diagnostics, environment=environment)

    def get(self, name: str) -> SkillMetadata:
        """Return one skill by name.

        Raises:
            KeyError: If no loaded skill matches *name*.
        """
        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(f"Skill not found: {name}") from None

    def list_all(self) -> list[SkillMetadata]:
        """Return all loaded skills sorted by name."""
        return [self._skills[name] for name in sorted(self._skills)]

    def diagnostics(self) -> list[SkillDiagnostic]:
        """Return diagnostics for loadable and rejected skill directories."""
        return sorted(
            self._diagnostics, key=lambda diagnostic: (diagnostic.name, str(diagnostic.path))
        )

    def invalid_diagnostics(self) -> list[SkillDiagnostic]:
        """Return diagnostics for rejected skill directories only."""
        return [diagnostic for diagnostic in self.diagnostics() if not diagnostic.loadable]

    def warnings_for(self, name: str) -> list[str]:
        """Return validation warnings for a loaded skill by name."""
        return [
            warning
            for diagnostic in self._diagnostics
            if diagnostic.name == name and diagnostic.loadable
            for warning in diagnostic.warnings
        ]

    def filter_allowed(self, allowed_skills: list[str]) -> list[SkillMetadata]:
        """Return available skills visible to an agent's ``allowed_skills`` setting.

        ``["*"]`` exposes every skill, ``[]`` exposes none, and any other list
        exposes only exact skill-name matches.  Unknown allowlist entries are
        ignored because skills are prompt metadata, not hard execution gates.
        Skills with unmet vBot requirements remain loadable but are not returned
        for prompt/tool visibility.
        """
        allowed_names = self._allowed_names(allowed_skills)
        return [
            skill
            for skill in self.list_all()
            if skill.name in allowed_names
            and self.availability_for(skill.name, allowed_skills).state == "available"
        ]

    def is_allowed(self, name: str, allowed_skills: Sequence[str] | None) -> bool:
        """Return whether a loaded skill is visible through an allowlist."""

        return name in self._allowed_names(allowed_skills)

    def availability_for(
        self,
        name: str,
        allowed_skills: Sequence[str] | None = None,
    ) -> SkillAvailability:
        """Return the runtime availability of a loadable skill."""

        skill = self._skills.get(name)
        if skill is None:
            return SkillAvailability("invalid", (f"skill '{name}' is not loadable",), ())

        allowed_names = self._allowed_names(allowed_skills)
        return self._availability_for_skill(skill, allowed_names, stack=())

    def _allowed_names(self, allowed_skills: Sequence[str] | None) -> set[str]:
        if allowed_skills is None or WILDCARD_ALLOWLIST in allowed_skills:
            return set(self._skills)
        return {name for name in allowed_skills if name in self._skills}

    def _availability_for_skill(
        self,
        skill: SkillMetadata,
        allowed_names: set[str],
        *,
        stack: tuple[str, ...],
    ) -> SkillAvailability:
        if skill.name in stack:
            cycle = " -> ".join((*stack, skill.name))
            return SkillAvailability("unavailable", (f"skill dependency cycle: {cycle}",), ())

        next_stack = (*stack, skill.name)
        missing: tuple[str, ...] = ()
        if skill.requirements.required is not None:
            required = self._evaluate_requirement(
                skill.requirements.required, allowed_names, next_stack
            )
            missing = required.missing

        optional_missing = tuple(
            missing_requirement
            for optional in skill.requirements.optional
            for missing_requirement in self._evaluate_requirement(
                optional,
                allowed_names,
                next_stack,
            ).missing
        )
        if missing:
            return SkillAvailability("unavailable", missing, optional_missing)
        if optional_missing:
            return SkillAvailability("available", (), optional_missing)
        return AVAILABLE

    def _evaluate_requirement(
        self,
        requirement: RequirementNode,
        allowed_names: set[str],
        stack: tuple[str, ...],
    ) -> RequirementEvaluation:
        if isinstance(requirement, RequirementCheck):
            return self._evaluate_requirement_check(requirement, allowed_names, stack)
        return self._evaluate_requirement_group(requirement, allowed_names, stack)

    def _evaluate_requirement_check(
        self,
        requirement: RequirementCheck,
        allowed_names: set[str],
        stack: tuple[str, ...],
    ) -> RequirementEvaluation:
        if requirement.kind == "binary":
            search_path = self._environment.get("PATH")
            if shutil.which(requirement.name, path=search_path) is not None:
                return RequirementEvaluation(True)
            return RequirementEvaluation(False, (f"missing binary '{requirement.name}'",))

        if requirement.kind == "env":
            if self._environment.get(requirement.name):
                return RequirementEvaluation(True)
            return RequirementEvaluation(
                False,
                (f"missing environment variable '{requirement.name}'",),
            )

        dependency = self._skills.get(requirement.name)
        if dependency is None:
            return RequirementEvaluation(False, (f"missing skill '{requirement.name}'",))
        if requirement.name not in allowed_names:
            return RequirementEvaluation(
                False,
                (f"skill '{requirement.name}' is not allowed for this agent",),
            )
        availability = self._availability_for_skill(dependency, allowed_names, stack=stack)
        if availability.state == "available":
            return RequirementEvaluation(True)
        details = "; ".join(availability.missing) or availability.state
        return RequirementEvaluation(
            False, (f"skill '{requirement.name}' is unavailable: {details}",)
        )

    def _evaluate_requirement_group(
        self,
        requirement: RequirementGroup,
        allowed_names: set[str],
        stack: tuple[str, ...],
    ) -> RequirementEvaluation:
        evaluations = [
            self._evaluate_requirement(child, allowed_names, stack)
            for child in requirement.children
        ]
        if requirement.operator == "all":
            missing = tuple(missing for evaluation in evaluations for missing in evaluation.missing)
            return RequirementEvaluation(not missing, missing)

        if any(evaluation.satisfied for evaluation in evaluations):
            return RequirementEvaluation(True)
        alternatives = ", ".join(child.describe() for child in requirement.children)
        return RequirementEvaluation(False, (f"requires one of: {alternatives}",))


def _load_skill_root(
    skills_dir: Path,
    skills: dict[str, SkillMetadata],
    diagnostics: list[SkillDiagnostic],
) -> None:
    if not skills_dir.is_dir():
        return

    for skill_dir in sorted(skills_dir.iterdir(), key=lambda path: path.name):
        if not skill_dir.is_dir():
            continue

        skill_file = skill_dir / SKILL_FILENAME
        if not skill_file.is_file():
            continue

        try:
            skill, result = _read_skill_metadata(skill_file)
        except OSError as exc:
            warnings = [f"Cannot read skill metadata {skill_file}: {exc}"]
            diagnostics.append(
                SkillDiagnostic(
                    name=skill_dir.name,
                    path=skill_file.resolve(),
                    valid=False,
                    warnings=warnings,
                    loadable=False,
                )
            )
            _log_validation_warnings(skill_dir.name, warnings)
            continue
        except ValueError as exc:
            warnings = [str(exc)]
            diagnostics.append(
                SkillDiagnostic(
                    name=skill_dir.name,
                    path=skill_file.resolve(),
                    valid=False,
                    warnings=warnings,
                    loadable=False,
                )
            )
            _log_validation_warnings(skill_dir.name, warnings)
            continue

        if skill is None:
            diagnostics.append(
                SkillDiagnostic(
                    name=skill_dir.name,
                    path=skill_file.resolve(),
                    valid=False,
                    warnings=result.warnings,
                    loadable=False,
                )
            )
            _log_validation_warnings(skill_dir.name, result.warnings)
            continue

        if skill.name in skills:
            warnings = [
                *result.warnings,
                (
                    f"Duplicate skill name '{skill.name}' rejected; "
                    f"first found at {skills[skill.name].path}."
                ),
            ]
            diagnostics.append(
                SkillDiagnostic(
                    name=skill.name,
                    path=skill.path,
                    valid=False,
                    warnings=warnings,
                    loadable=False,
                )
            )
            _log_validation_warnings(skill.name, warnings)
            continue

        skills[skill.name] = skill
        diagnostics.append(
            SkillDiagnostic(
                name=skill.name,
                path=skill.path,
                valid=len(result.warnings) == 0,
                warnings=result.warnings,
                loadable=True,
            )
        )
        _log_validation_warnings(skill.name, result.warnings)


def _read_skill_metadata(skill_file: Path) -> tuple[SkillMetadata | None, ValidationResult]:
    content = skill_file.read_text(encoding="utf-8")
    front_matter = _extract_front_matter(content, skill_file)
    fields, parse_warnings = _parse_front_matter(front_matter, skill_file)
    result = validate_skill_metadata(
        fields,
        directory_name=skill_file.parent.name,
        skill_file=skill_file,
        parse_warnings=parse_warnings,
    )
    if not result.valid or not isinstance(fields, dict):
        return None, result

    name = _field_to_string(fields.get("name"))
    description = _field_to_string(fields.get("description"))
    metadata = _optional_mapping(fields.get("metadata"))
    try:
        requirements = parse_vbot_requirements(metadata)
    except RequirementParseError as exc:
        return None, ValidationResult(valid=False, warnings=[str(exc)])

    return (
        SkillMetadata(
            name=name,
            description=description,
            path=skill_file.resolve(),
            license=_optional_string(fields.get("license")),
            compatibility=fields.get("compatibility"),
            metadata=metadata,
            allowed_tools=_optional_string_list(fields.get("allowed-tools")),
            requirements=requirements,
        ),
        result,
    )


def _extract_front_matter(content: str, skill_file: Path) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise ValueError(f"Skill metadata missing front matter: {skill_file}")

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONT_MATTER_DELIMITER:
            return "\n".join(lines[1:index])

    raise ValueError(f"Skill metadata front matter is not closed: {skill_file}")


def _parse_front_matter(front_matter: str, skill_file: Path) -> tuple[Any, list[str]]:
    try:
        return yaml.safe_load(front_matter) or {}, []
    except yaml.YAMLError:
        repaired = repair_colon_scalars(front_matter)
        if repaired == front_matter:
            return None, [f"Invalid YAML front matter: {skill_file}"]
        try:
            return yaml.safe_load(repaired) or {}, [MALFORMED_YAML_FALLBACK_WARNING]
        except yaml.YAMLError:
            return None, [f"Invalid YAML front matter: {skill_file}"]


def _scan_skill_resources(skill_dir: Path) -> list[str]:
    """Return relative file paths under activation-time skill resource directories."""
    resources: list[str] = []
    for resource_directory in RESOURCE_DIRECTORIES:
        root = skill_dir / resource_directory
        if not root.is_dir():
            continue
        for resource_path in sorted(path for path in root.rglob("*") if path.is_file()):
            resources.append(resource_path.relative_to(skill_dir).as_posix())
    return resources


def _field_to_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _optional_string(value: Any) -> str | None:
    text = _field_to_string(value)
    return text or None


def _optional_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _optional_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def project_skills_dir(project_cwd: Path) -> Path:
    """Return a project's own skill directory (``<cwd>/.opencode/skills/``)."""
    return project_cwd.joinpath(*PROJECT_SKILLS_SUBPATH)


def load_project_skill_registry(
    project_cwd: Path,
    bundled_scan_roots: Sequence[Path],
    environment: Mapping[str, str] | None = None,
) -> SkillRegistry:
    """Build a project-scoped registry: the project's own skills, then the bundled ones.

    The project skill directory is scanned **first** so a project skill wins a name
    collision with a bundled skill of the same name (one slot, the project's own
    playbook wins). ``bundled_scan_roots`` must be the same ordered roots the global
    registry scans, so a project run sees exactly the bundled pool plus its own
    skills — nothing leaks between projects. A missing project skill directory is
    treated as empty, so a project without ``.opencode/skills/`` simply gets the
    bundled pool.
    """
    return SkillRegistry.load(
        project_skills_dir(project_cwd),
        extra_dirs=list(bundled_scan_roots),
        environment=environment,
    )


def scan_project_skill_names(
    project_cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return the names of the skills defined in a project's own skill directory.

    Scans only ``<cwd>/.opencode/skills/`` (not the bundled roots), so the result is
    exactly the project-owned skills — the set the resolver subtracts
    ``skills_project_disabled`` from when computing a config agent's effective
    skills. A missing directory yields an empty set.
    """
    project_only = SkillRegistry.load(project_skills_dir(project_cwd), environment=environment)
    return frozenset(skill.name for skill in project_only.list_all())


def _log_validation_warnings(skill_name: str, warnings: list[str]) -> None:
    for warning in warnings:
        _LOGGER.warning("Skill '%s' metadata warning: %s", skill_name, warning)
