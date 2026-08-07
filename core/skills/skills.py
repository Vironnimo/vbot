"""Skill metadata registry for local agent skills.

Skills are reusable playbooks stored under ``<data_dir>/skills/<skill-id>/``.
Each skill directory must contain a ``SKILL.md`` file. Project-owned skills live
in the skill directory of the project's declared source format
(:data:`PROJECT_SKILLS_SUBPATHS`). The registry reads the Markdown front matter
for prompt metadata and filters it through an agent's ``allowed_skills`` list.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

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
    ValidationResult,
    normalize_and_validate_skill_metadata,
    parse_skill_front_matter,
    split_skill_document,
)
from core.utils.logging import get_logger

WILDCARD_ALLOWLIST = "*"
SKILL_FILENAME = "SKILL.md"
RESOURCE_DIRECTORIES = ("scripts", "references", "assets")
# A project's own skills live beside its agents, in the skill directory of the
# project's declared source format (GLOSSARY → Source Format) — one entry per
# format, keyed by the canonical ``core.settings.PROJECT_SOURCE_FORMATS`` values
# so the two vocabularies can never drift. Scanned per project and merged with
# the bundled skills, project-first so a project skill wins a name collision
# with a bundled one (decision 3/4 in the whitelist plan).
PROJECT_SKILLS_SUBPATHS: dict[str, tuple[str, ...]] = {
    "opencode": (".opencode", "skills"),
    "claude": (".claude", "skills"),
}

# Origin tags identify which scope a loaded skill came from, so the prompt catalog
# and the ``skill`` tool can group skills by where they live. They are opaque
# strings stored per skill at load (the runtime supplies them per scan root); the
# skills domain only records and orders them — the human-facing labels live in the
# prompt layer. A project's tag carries its display name after the prefix.
SKILL_ORIGIN_AGENT = "agent"
SKILL_ORIGIN_GLOBAL = "global"
SKILL_ORIGIN_BUNDLED = "bundled"
SKILL_ORIGIN_PROJECT_PREFIX = "project:"

_LOGGER = get_logger("skills")


def project_skill_origin(project_display_name: str) -> str:
    """Return the origin tag for a project's own skills, carrying its display name."""
    return f"{SKILL_ORIGIN_PROJECT_PREFIX}{project_display_name}"


def skill_origin_sort_key(origin: str | None) -> tuple[int, str]:
    """Order origins for catalog/list grouping: bundled, global, project(s), agent.

    Within the project tier, group by display name. Unknown/absent origins sort
    last so a registry built without origin tags still renders deterministically.
    """
    if origin == SKILL_ORIGIN_BUNDLED:
        return (0, "")
    if origin == SKILL_ORIGIN_GLOBAL:
        return (1, "")
    if origin is not None and origin.startswith(SKILL_ORIGIN_PROJECT_PREFIX):
        return (2, origin[len(SKILL_ORIGIN_PROJECT_PREFIX) :])
    if origin == SKILL_ORIGIN_AGENT:
        return (3, "")
    return (4, origin or "")


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
    # The scope this skill was scanned from (see the ``SKILL_ORIGIN_*`` tags), set
    # at load by the registry from its scan-root origins. ``None`` when the loader
    # was given no origins (e.g. a single-directory scan), which renders ungrouped.
    origin: str | None = None


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
        always_allowed: Iterable[str] | None = None,
    ) -> None:
        self._skills = skills
        self._diagnostics = list(diagnostics or [])
        self._environment = dict(os.environ if environment is None else environment)
        # Names that bypass an agent's ``allowed_skills`` filter for *this* registry
        # only. The runtime sets this to an agent's own private skills, so an
        # agent-scoped registry always exposes the agent's own skills while a
        # shared registry (global/project) leaves it empty and filters as before.
        self._always_allowed = frozenset(always_allowed or ())

    @classmethod
    def load(
        cls,
        skills_dir: Path,
        extra_dirs: list[Path] | None = None,
        environment: Mapping[str, str] | None = None,
        always_allowed: Iterable[str] | None = None,
        origins: Sequence[str | None] | None = None,
    ) -> SkillRegistry:
        """Load all valid skills from immediate subdirectories of scan roots.

        Missing skill roots are treated as empty.  A directory is a skill only
        when it contains ``SKILL.md`` with loadable YAML front matter.  When
        duplicate names are found, the first scanned directory wins and the
        rejected duplicate is preserved as a diagnostic.  ``always_allowed`` names
        bypass the ``allowed_skills`` filter for this registry (the runtime passes
        an agent's own private skills so they are always visible to their owner).
        ``origins`` is a parallel sequence of origin tags for ``[skills_dir,
        *extra_dirs]``; each loaded skill records the tag of the root it came from
        (missing/short → ``None``), so the catalog can group by scope.
        """
        skills: dict[str, SkillMetadata] = {}
        diagnostics: list[SkillDiagnostic] = []
        scan_roots = [skills_dir, *(extra_dirs or [])]
        origin_tags = list(origins) if origins is not None else []
        for index, scan_root in enumerate(scan_roots):
            origin = origin_tags[index] if index < len(origin_tags) else None
            _load_skill_root(scan_root, skills, diagnostics, origin)

        return cls(skills, diagnostics, environment=environment, always_allowed=always_allowed)

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
        allowed = {name for name in allowed_skills if name in self._skills}
        allowed |= {name for name in self._always_allowed if name in self._skills}
        return allowed

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
    origin: str | None = None,
) -> None:
    if not skills_dir.is_dir():
        return

    try:
        skill_directories = sorted(skills_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        warnings = [f"Cannot scan skill directory {skills_dir}: {exc}"]
        diagnostics.append(
            SkillDiagnostic(
                name=skills_dir.name,
                path=skills_dir,
                valid=False,
                warnings=warnings,
                loadable=False,
            )
        )
        _log_validation_warnings(skills_dir.name, skills_dir, warnings)
        return

    for skill_dir in skill_directories:
        if not skill_dir.is_dir():
            continue

        skill_file = skill_dir / SKILL_FILENAME
        if not skill_file.is_file():
            continue

        resolved_skill_file = skill_file.resolve()
        try:
            skill, result = _read_skill_metadata(skill_file)
        except OSError as exc:
            warnings = [f"Cannot read skill metadata {skill_file}: {exc}"]
            diagnostics.append(
                SkillDiagnostic(
                    name=skill_dir.name,
                    path=resolved_skill_file,
                    valid=False,
                    warnings=warnings,
                    loadable=False,
                )
            )
            _log_validation_warnings(skill_dir.name, resolved_skill_file, warnings)
            continue
        except ValueError as exc:
            warnings = [str(exc)]
            diagnostics.append(
                SkillDiagnostic(
                    name=skill_dir.name,
                    path=resolved_skill_file,
                    valid=False,
                    warnings=warnings,
                    loadable=False,
                )
            )
            _log_validation_warnings(skill_dir.name, resolved_skill_file, warnings)
            continue

        if skill is None:
            diagnostics.append(
                SkillDiagnostic(
                    name=skill_dir.name,
                    path=resolved_skill_file,
                    valid=False,
                    warnings=result.warnings,
                    loadable=False,
                )
            )
            _log_validation_warnings(skill_dir.name, resolved_skill_file, result.warnings)
            continue

        skill = replace(skill, origin=origin)
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
            _log_validation_warnings(skill.name, skill.path, warnings)
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
        _log_validation_warnings(skill.name, skill.path, result.warnings)


def _read_skill_metadata(skill_file: Path) -> tuple[SkillMetadata | None, ValidationResult]:
    content = skill_file.read_text(encoding="utf-8")
    front_matter, body, document_warnings = split_skill_document(content)
    fields, parse_warnings = parse_skill_front_matter(front_matter)
    fields, result = normalize_and_validate_skill_metadata(
        fields,
        directory_name=skill_file.parent.name,
        skill_file=skill_file,
        body=body,
        parse_warnings=[*document_warnings, *parse_warnings],
    )
    if not result.valid:
        return None, result

    name = _field_to_string(fields.get("name"))
    description = _field_to_string(fields.get("description"))
    metadata = _optional_mapping(fields.get("metadata"))
    try:
        requirements = parse_vbot_requirements(metadata)
    except RequirementParseError as exc:
        return None, ValidationResult(valid=False, warnings=[*result.warnings, str(exc)])

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


def project_skills_dir(project_cwd: Path, source_format: str) -> Path:
    """Return a project's own skill directory for its declared source format.

    ``<cwd>/.opencode/skills/`` for an OpenCode project, ``<cwd>/.claude/skills/``
    for a Claude Code one. The format is a required argument — every caller must
    say which format's directory it means; an unknown format raises ``KeyError``
    (callers only pass the validated ``Project.source_format``).
    """
    return project_cwd.joinpath(*PROJECT_SKILLS_SUBPATHS[source_format])


def load_project_skill_registry(
    project_cwd: Path,
    source_format: str,
    bundled_scan_roots: Sequence[Path],
    environment: Mapping[str, str] | None = None,
    *,
    project_origin: str | None = None,
    bundled_origins: Sequence[str | None] | None = None,
) -> SkillRegistry:
    """Build a project-scoped registry: the project's own skills, then the bundled ones.

    The project skill directory — the declared ``source_format``'s location — is
    scanned **first** so a project skill wins a name collision with a bundled skill
    of the same name (one slot, the project's own playbook wins).
    ``bundled_scan_roots`` must be the same ordered roots the global registry scans,
    so a project run sees exactly the bundled pool plus its own skills — nothing
    leaks between projects. A missing project skill directory is treated as empty,
    so a project without one simply gets the bundled pool.
    ``project_origin``/``bundled_origins`` tag the loaded skills with their scope
    for catalog grouping (the project root then the bundled roots).
    """
    origins: list[str | None] | None = None
    if project_origin is not None or bundled_origins is not None:
        bundled = (
            list(bundled_origins)
            if bundled_origins is not None
            else [None] * len(bundled_scan_roots)
        )
        origins = [project_origin, *bundled]
    return SkillRegistry.load(
        project_skills_dir(project_cwd, source_format),
        extra_dirs=list(bundled_scan_roots),
        environment=environment,
        origins=origins,
    )


def scan_skill_names(
    skills_dir: Path,
    environment: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return the names of the skills defined directly under one skill directory.

    Scans only ``skills_dir`` (no extra/bundled roots), so the result is exactly
    the skills that directory owns. A missing directory yields an empty set. The
    runtime uses this for both a project's own skills and an agent's private
    skills home.
    """
    registry = SkillRegistry.load(skills_dir, environment=environment)
    return frozenset(skill.name for skill in registry.list_all())


def scan_project_skill_names(
    project_cwd: Path,
    source_format: str,
    environment: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return the names of the skills defined in a project's own skill directory.

    Scans only the declared format's skill directory (not the bundled roots), so
    the result is exactly the project-owned skills — the set the resolver subtracts
    ``skills_project_disabled`` from when computing a config agent's effective
    skills. A missing directory yields an empty set.
    """
    return scan_skill_names(project_skills_dir(project_cwd, source_format), environment)


# Skill registries are reloaded often — once per project, per run, and on every
# explicit reload — so re-logging a skill's metadata diagnostic on each scan floods
# debug logs. Track the (path, warning) pairs already logged in this process and
# emit each one once: "once per server runtime". A server restart starts a fresh
# process and logs the current diagnostics again. This is a process-scoped logging
# concern (it lives beside the module logger it guards), not an injectable
# service — the diagnostics returned to callers are never deduplicated, so the UI
# still sees every warning on every load.
_logged_skill_warnings: set[tuple[str, str]] = set()


def _log_validation_warnings(skill_name: str, skill_path: Path, warnings: list[str]) -> None:
    for warning in warnings:
        dedup_key = (str(skill_path), warning)
        if dedup_key in _logged_skill_warnings:
            continue
        _logged_skill_warnings.add(dedup_key)
        _LOGGER.debug("Skill '%s' metadata diagnostic: %s (at %s)", skill_name, warning, skill_path)
