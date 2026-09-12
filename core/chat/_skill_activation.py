"""Deterministic Skill triggers and active Session environment grants."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from core.sessions import ChatSession
from core.skills.requirements import SkillRequirements, environment_requirement_names
from core.skills.skill_validator import SKILL_NAME_CHARSET_FRAGMENT
from core.tools.skill import load_skill_content
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.skills.skills import SkillRegistry


_LOGGER = get_logger("chat")

# Trigger names use the same grammar enforced by Skill authoring.
SKILL_SLASH_TRIGGER_PATTERN = re.compile(rf"^/({SKILL_NAME_CHARSET_FRAGMENT})(?=\s|$)")


SKILL_INLINE_TRIGGER_PATTERN = re.compile(rf"\$({SKILL_NAME_CHARSET_FRAGMENT})")


def _active_skill_env_keys(
    session: ChatSession,
    skill_registry: SkillRegistry | None,
) -> tuple[str, ...]:
    """Return Env grants declared by Skills active in the current Session state."""
    if skill_registry is None:
        return ()
    names: list[str] = []
    for skill_name in session.activated_skill_contents():
        try:
            skill = skill_registry.get(skill_name)
        except KeyError:
            continue
        names.extend(environment_requirement_names(skill.requirements))
    return tuple(dict.fromkeys(names))


def _activate_triggered_skills(
    agent: Any,
    session: ChatSession,
    content: str,
    skill_registry: SkillRegistry,
) -> None:
    if not _triggered_skill_names(content):
        return

    allowed_skills = getattr(agent, "allowed_skills", None)
    if allowed_skills is None:
        allowed_skills = ["*"]
    allowed_by_name = _allowed_loadable_skills(skill_registry, allowed_skills)
    for skill_name in _triggered_skill_names(content):
        skill = allowed_by_name.get(skill_name)
        if skill is None:
            _LOGGER.warning(
                "Ignored skill trigger '%s' for agent=%s session=%s "
                "because it is not allowed or loadable",
                skill_name,
                agent.id,
                session.id,
            )
            session.add_note(
                f"Skill trigger '{skill_name}' did not match an allowed loadable skill."
            )
            continue
        unavailable_reason = _unavailable_skill_reason(
            skill_registry,
            skill_name,
            allowed_skills,
        )
        if unavailable_reason is not None:
            _LOGGER.warning(
                "Ignored skill trigger '%s' for agent=%s session=%s because it is unavailable: %s",
                skill_name,
                agent.id,
                session.id,
                unavailable_reason,
            )
            session.add_note(
                f"Skill trigger '{skill_name}' matched a skill, but it is unavailable: "
                f"{unavailable_reason}"
            )
            continue
        try:
            data = load_skill_content(
                skill.name,
                skill.path,
                env_keys=environment_requirement_names(
                    getattr(skill, "requirements", SkillRequirements())
                ),
            )
        except OSError as error:
            _LOGGER.warning(
                "Failed to load triggered skill '%s' for agent=%s session=%s: %s",
                skill_name,
                agent.id,
                session.id,
                error,
            )
            session.add_note(f"Skill trigger '{skill_name}' could not be loaded: {error}")
            continue
        except ValueError as error:
            _LOGGER.warning(
                "Failed to parse triggered skill '%s' for agent=%s session=%s: %s",
                skill_name,
                agent.id,
                session.id,
                error,
            )
            session.add_note(f"Skill trigger '{skill_name}' could not be loaded: {error}")
            continue
        if session.activate_skill_context(skill.name, data):
            _LOGGER.info(
                "Activated triggered skill '%s' for agent=%s session=%s",
                skill.name,
                agent.id,
                session.id,
            )


def _allowed_loadable_skills(
    skill_registry: SkillRegistry,
    allowed_skills: list[str],
) -> dict[str, Any]:
    return {
        skill.name: skill
        for skill in skill_registry.list_all()
        if skill_registry.is_allowed(skill.name, allowed_skills)
    }


def _unavailable_skill_reason(
    skill_registry: SkillRegistry,
    skill_name: str,
    allowed_skills: list[str],
) -> str | None:
    availability = skill_registry.availability_for(skill_name, allowed_skills)
    if availability.state == "available":
        return None
    missing = list(availability.missing)
    return "; ".join(missing) if missing else str(availability.state)


def _triggered_skill_names(content: str) -> list[str]:
    names: list[str] = []
    slash_match = SKILL_SLASH_TRIGGER_PATTERN.search(content)
    if slash_match:
        names.append(slash_match.group(1))

    for inline_match in SKILL_INLINE_TRIGGER_PATTERN.finditer(content):
        name = inline_match.group(1)
        if name not in names:
            names.append(name)
    return names
