"""Skill metadata registry for local agent skills.

Skills are reusable playbooks stored under ``<data_dir>/skills/<skill-id>/``.
Each skill directory must contain a ``SKILL.md`` file.  The registry reads the
Markdown front matter for prompt metadata and filters it through an agent's
``allowed_skills`` list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FRONT_MATTER_DELIMITER = "---"
WILDCARD_ALLOWLIST = "*"


@dataclass(frozen=True)
class SkillMetadata:
    """Prompt metadata for a local skill."""

    name: str
    description: str
    path: Path


class SkillRegistry:
    """Scans local skill directories and filters prompt-visible metadata."""

    def __init__(self, skills: dict[str, SkillMetadata]) -> None:
        self._skills = skills

    @classmethod
    def load(cls, skills_dir: Path) -> SkillRegistry:
        """Load all valid skills from immediate subdirectories of *skills_dir*.

        Missing skill roots are treated as an empty registry.  A directory is a
        skill only when it contains ``SKILL.md`` with a front-matter ``name`` and
        ``description``.
        """
        skills: dict[str, SkillMetadata] = {}
        if not skills_dir.is_dir():
            return cls(skills)

        for skill_dir in sorted(skills_dir.iterdir(), key=lambda path: path.name):
            if not skill_dir.is_dir():
                continue

            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue

            skill = _read_skill_metadata(skill_file)
            if skill.name in skills:
                raise ValueError(f"Duplicate skill name: {skill.name}")
            skills[skill.name] = skill

        return cls(skills)

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

    def filter_allowed(self, allowed_skills: list[str]) -> list[SkillMetadata]:
        """Return skills visible to an agent's ``allowed_skills`` setting.

        ``["*"]`` exposes every skill, ``[]`` exposes none, and any other list
        exposes only exact skill-name matches.  Unknown allowlist entries are
        ignored because skills are prompt metadata, not hard execution gates.
        """
        if WILDCARD_ALLOWLIST in allowed_skills:
            return self.list_all()

        allowed = set(allowed_skills)
        return [skill for skill in self.list_all() if skill.name in allowed]


def _read_skill_metadata(skill_file: Path) -> SkillMetadata:
    content = skill_file.read_text(encoding="utf-8")
    front_matter = _extract_front_matter(content, skill_file)
    fields = _parse_front_matter(front_matter)

    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not name:
        raise ValueError(f"Skill metadata missing name: {skill_file}")
    if not description:
        raise ValueError(f"Skill metadata missing description: {skill_file}")

    return SkillMetadata(
        name=name,
        description=description,
        path=skill_file.resolve(),
    )


def _extract_front_matter(content: str, skill_file: Path) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise ValueError(f"Skill metadata missing front matter: {skill_file}")

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONT_MATTER_DELIMITER:
            return "\n".join(lines[1:index])

    raise ValueError(f"Skill metadata front matter is not closed: {skill_file}")


def _parse_front_matter(front_matter: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key = ""
    current_lines: list[str] = []

    for line in front_matter.splitlines():
        if line.startswith((" ", "\t")) and current_key:
            current_lines.append(line.strip())
            continue

        if current_key:
            fields[current_key] = " ".join(current_lines).strip()
            current_key = ""
            current_lines = []

        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        current_key = key.strip()
        current_lines = [_clean_field_value(value.strip())]

    if current_key:
        fields[current_key] = " ".join(current_lines).strip()

    return fields


def _clean_field_value(value: str) -> str:
    if value in (">", "|"):
        return ""
    return value.strip("\"'")
