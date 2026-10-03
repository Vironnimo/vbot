"""A Skill merged into another one: what named it moves to that Skill.

A ``skill_manage`` delete with ``absorbed_into`` says the deleted Skill's
instructions now live in another Skill of the same Identity Agent. Its shares
(Skill Policy) and the triggers in that Agent's live automations
(``AutomationReferences``) then name the other Skill, so receivers and scheduled
work keep getting those instructions. Runtime owns both, so it orders the
follow: under the automation reference lock it lists what names the Skill, lets
the delete record that list in the Skill history, and moves each reference once
the delete succeeded. Restoring the archived Skill moves nothing back.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.automation import AutomationReferences
from core.skills import SkillPolicyError, SkillPolicyService, SkillReference
from core.utils.logging import get_logger

_LOGGER = get_logger("runtime.skill_merge")


@dataclass(frozen=True)
class SkillMergeServices:
    """The owners a Skill merge changes, and how to name a receiver Agent."""

    policy: SkillPolicyService
    automation: AutomationReferences
    agent_name: Callable[[str], str]
    # Receivers see shared Skills through their registries, which a share move outdates.
    shares_changed: Callable[[], None]


async def follow_skill_merge(
    services: SkillMergeServices,
    owner_id: str,
    name: str,
    target: str,
    delete: Callable[[tuple[SkillReference, ...]], Awaitable[bool]],
) -> tuple[SkillReference, ...]:
    """Delete the Skill ``name`` of ``owner_id`` into ``target`` and move what named it there.

    ``delete(followed)`` deletes the Skill, recording ``followed``, and says
    whether it did. Returns the references that could not move; they still name
    ``name``.
    """
    automation = services.automation
    async with automation.lock:
        receivers = sorted(services.policy.load().shared.get(owner_id, {}).get(name, ()))
        automations = automation.agent_skill_triggers(owner_id, name)
        shares = tuple(
            SkillReference("shared", receiver, services.agent_name(receiver))
            for receiver in receivers
        )
        triggers = tuple(
            SkillReference(reference.kind, reference.id, reference.name)
            for reference in automations
        )
        if not await delete((*shares, *triggers)):
            return ()
        failed: list[SkillReference] = []
        if shares:
            try:
                services.policy.move_shared(owner_id, name, target)
            except SkillPolicyError as error:
                _LOGGER.error(
                    "Skill shares did not follow a merge (agent=%s skill=%s target=%s): %s",
                    owner_id,
                    name,
                    target,
                    error,
                )
                failed.extend(shares)
            else:
                services.shares_changed()
        for reference, followed in zip(automations, triggers, strict=True):
            try:
                await automation.rename_skill_triggers(owner_id, reference, name, target)
            except Exception as error:
                # Each automation owner refuses with its own error type.
                _LOGGER.error(
                    "Automation did not follow a Skill merge (agent=%s skill=%s target=%s "
                    "automation=%s): %s",
                    owner_id,
                    name,
                    target,
                    reference.label,
                    error,
                )
                failed.append(followed)
    if shares or triggers:
        _LOGGER.info(
            "Skill merge moved references (agent=%s skill=%s target=%s shares=%d "
            "automations=%d failed=%d)",
            owner_id,
            name,
            target,
            len(shares),
            len(triggers),
            len(failed),
        )
    return tuple(failed)
