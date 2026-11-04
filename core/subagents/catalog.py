"""Prompt-visible catalog of additional Sub-Agent targets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.projects import (
    AgentResolutionError,
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices

_LOGGER = get_logger("subagents")


@dataclass(frozen=True)
class SubAgentPromptTarget:
    """One additional Agent the caller may select explicitly in a Tool call."""

    agent_id: str
    name: str
    description: str = ""


def build_subagent_prompt_targets(
    runtime: RuntimeServices,
    agent: Any,
    project_id: str | None,
) -> list[SubAgentPromptTarget]:
    """Return the caller's additional targets in deterministic Tool-call form.

    The calling Agent is deliberately absent: self-delegation is implicit whenever
    the ``subagent`` Tool is active and is selected by omitting ``agent_id``. A
    Project Agent sees allowed peers from its own cached Team as bare ids. An
    Identity Agent sees allowed Identity Agents as bare ids and Project Agents as
    qualified ``agent@project`` ids.
    """
    from core.tools.availability import agent_tool_settings

    return subagent_targets(
        runtime,
        agent.id,
        project_id,
        agent_tool_settings(agent.tools),
        delegation_bindings=getattr(agent, "delegation_bindings", {}),
    )


def subagent_targets(
    runtime: RuntimeServices,
    caller_agent_id: str,
    project_id: str | None,
    tool_settings: Any,
    *,
    delegation_bindings: Mapping[str, str | int] | None = None,
) -> list[SubAgentPromptTarget]:
    """Return the additional targets a caller with these Tool settings may select.

    The same catalog the System Prompt block shows, so an error can name the
    exact choices without revealing Agents the caller cannot use.
    """
    from core.tools.availability import subagent_allowed_agents

    allowed = subagent_allowed_agents(tool_settings)
    if project_id is not None:
        return _project_targets(runtime, caller_agent_id, project_id, allowed)
    return _identity_targets(runtime, caller_agent_id, allowed, delegation_bindings or {})


def _project_targets(
    runtime: RuntimeServices,
    caller_agent_id: str,
    project_id: str,
    allowed: list[str],
) -> list[SubAgentPromptTarget]:
    allowed_set = set(allowed)
    wildcard = "*" in allowed_set
    targets: list[SubAgentPromptTarget] = []
    for member in runtime.agent_resolver.team_for_project(project_id):
        if member.agent_id == caller_agent_id:
            continue
        if not wildcard and member.agent_id not in allowed_set:
            continue
        targets.append(
            SubAgentPromptTarget(
                agent_id=member.agent_id,
                name=member.display_name,
                description=member.description,
            )
        )
    return sorted(targets, key=lambda target: target.agent_id)


def _identity_targets(
    runtime: RuntimeServices,
    caller_agent_id: str,
    allowed: list[str],
    delegation_bindings: Mapping[str, str | int],
) -> list[SubAgentPromptTarget]:
    wildcard = "*" in allowed
    roster = runtime.agents.list()
    identities = {agent.id: agent for agent in roster}
    targets: dict[str, SubAgentPromptTarget] = {}
    candidates = set(identities) if wildcard else set()
    candidates.update(target for target in allowed if "@" not in target and target != "*")
    candidates.update(runtime.agents.automatic_targets(caller_agent_id, roster))
    for target_id in candidates - {caller_agent_id}:
        identity = identities.get(target_id)
        if identity is None:
            continue
        binding = delegation_bindings.get(target_id)
        listed = target_id in allowed and (
            binding is None or runtime.agents.delegation_target_allowed(target_id, binding)
        )
        if runtime.agents.placement_target_allowed(
            caller_agent_id, target_id, listed=listed, wildcard=wildcard, roster=roster
        ):
            targets[target_id] = SubAgentPromptTarget(
                target_id, identity.name, identity.description
            )

    projects = runtime.projects.list()
    if wildcard:
        for project in projects:
            try:
                team = runtime.agent_resolver.team_for_project(project.project_id)
            except (AgentResolutionError, OSError, ValueError) as error:
                _LOGGER.warning(
                    "Skipping unavailable Project %r in Sub-Agent prompt catalog: %s",
                    project.project_id,
                    error,
                )
                continue
            for member in team:
                if member.unavailable_reason:
                    continue
                qualified_id = format_agent_address(member.agent_id, project.project_id)
                targets[qualified_id] = SubAgentPromptTarget(
                    qualified_id, member.display_name, member.description
                )
    else:
        project_ids = {project.project_id for project in projects}
        for configured_target in allowed:
            try:
                target_id, project_id = parse_agent_address(configured_target)
            except InvalidAgentAddressError:
                _LOGGER.warning(
                    "Skipping invalid configured Sub-Agent target %r", configured_target
                )
                continue
            if project_id is None or project_id not in project_ids:
                continue
            configured_member = _project_member(runtime, project_id, target_id)
            if configured_member is not None:
                qualified_id = format_agent_address(target_id, project_id)
                targets[qualified_id] = SubAgentPromptTarget(
                    qualified_id, configured_member.display_name, configured_member.description
                )
    return [targets[target_id] for target_id in sorted(targets)]


def _project_member(runtime: RuntimeServices, project_id: str, agent_id: str) -> Any | None:
    try:
        team = runtime.agent_resolver.team_for_project(project_id)
    except (AgentResolutionError, OSError, ValueError) as error:
        _LOGGER.warning(
            "Skipping unavailable Project %r in Sub-Agent prompt catalog: %s",
            project_id,
            error,
        )
        return None
    return next((member for member in team if member.agent_id == agent_id), None)


__all__ = ["SubAgentPromptTarget", "build_subagent_prompt_targets", "subagent_targets"]
