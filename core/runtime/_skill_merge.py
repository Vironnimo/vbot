"""A deleted Skill: what named it moves to the Skill that absorbed it, if any.

A ``skill_manage`` delete with ``absorbed_into`` says the deleted Skill's
instructions now live in another Skill of the same Identity Agent. Its shares
(Skill Policy) and the triggers in that Agent's live automations
(``AutomationReferences``) then name the other Skill, so receivers and scheduled
work keep getting those instructions. Runtime owns both, so it orders the
follow: under the automation reference lock it lists what names the Skill, lets
the delete record that list in the Skill history, and moves each reference once
the delete succeeded. Restoring the archived Skill moves nothing back.

A delete without ``absorbed_into`` moves nothing: it returns what still names
the deleted Skill, so the caller can say so.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from core.automation import AutomationReferences
from core.skills import SkillPolicyError, SkillPolicyService, SkillReference
from core.tools.tools import run_tool_worker
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
    mutation_guard: Callable[[], AbstractContextManager[None]]
    prepare_access: Callable[[str, str, str], tuple[str, ...]]
    preserve_access: Callable[[str, str], None]


async def follow_skill_merge(
    services: SkillMergeServices,
    owner_id: str,
    name: str,
    target: str | None,
    delete: Callable[[tuple[SkillReference, ...]], bool],
) -> tuple[SkillReference, ...]:
    """Delete the Skill ``name`` of ``owner_id`` into ``target`` and move what named it there.

    ``delete(followed)`` deletes the Skill, recording ``followed``, and says
    whether it did. Returns the references that did not move; they still name
    ``name``. Without ``target`` nothing moves: every reference is returned.
    """
    automation = services.automation
    async with automation.lock:
        automations = automation.agent_skill_triggers(owner_id, name)
        triggers = tuple(
            SkillReference(reference.kind, reference.id, reference.name)
            for reference in automations
        )

        def prepare_and_delete() -> tuple[tuple[SkillReference, ...], tuple[str, ...], bool]:
            with services.mutation_guard():
                receivers = sorted(services.policy.load().shared.get(owner_id, {}).get(name, ()))
                shares = tuple(
                    SkillReference("shared", receiver, services.agent_name(receiver))
                    for receiver in receivers
                )
                permitted = (
                    services.prepare_access(owner_id, name, target) if target is not None else ()
                )
                deleted = delete((*shares, *triggers) if target is not None else ())
                return shares, permitted, deleted

        shares, permitted, deleted = await run_tool_worker(prepare_and_delete)
        if not deleted:
            return ()
        if target is None:
            return (*shares, *triggers)
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
        for agent_id in permitted:
            # A failed share move has already been reported. Granting the name
            # then could select an unrelated package in the receiver's pool.
            if any(reference.id == agent_id for reference in failed):
                continue
            try:
                await run_tool_worker(services.preserve_access, agent_id, target)
            except Exception as error:
                _LOGGER.error(
                    "Agent Skill access did not follow a merge (agent=%s skill=%s target=%s): %s",
                    agent_id,
                    name,
                    target,
                    error,
                    exc_info=True,
                )
                failed.append(SkillReference("agent", agent_id, services.agent_name(agent_id)))
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
    if shares or triggers or permitted:
        _LOGGER.info(
            "Skill merge moved references (agent=%s skill=%s target=%s shares=%d "
            "automations=%d access=%d failed=%d)",
            owner_id,
            name,
            target,
            len(shares),
            len(triggers),
            len(permitted),
            len(failed),
        )
    return tuple(failed)
