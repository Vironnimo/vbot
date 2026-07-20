"""Prompt-visible catalog of additional Sub-Agent targets."""

from __future__ import annotations

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
    from core.tools.availability import agent_tool_settings, subagent_allowed_agents

    allowed = subagent_allowed_agents(agent_tool_settings(agent.tools))
    if project_id is not None:
        return _project_targets(runtime, agent.id, project_id, allowed)
    return _identity_targets(runtime, agent.id, allowed)


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
) -> list[SubAgentPromptTarget]:
    if "*" in allowed:
        return _all_identity_scope_targets(runtime, caller_agent_id)

    identities = {agent.id: agent for agent in runtime.agents.list()}
    projects = {project.project_id: project for project in runtime.projects.list()}
    targets: dict[str, SubAgentPromptTarget] = {}
    for configured_target in allowed:
        try:
            target_agent_id, target_project_id = parse_agent_address(configured_target)
        except InvalidAgentAddressError:
            _LOGGER.warning("Skipping invalid configured Sub-Agent target %r", configured_target)
            continue

        if target_project_id is None:
            if target_agent_id == caller_agent_id:
                continue
            identity = identities.get(target_agent_id)
            if identity is not None:
                targets[target_agent_id] = SubAgentPromptTarget(
                    agent_id=target_agent_id,
                    name=identity.name,
                )
            continue

        project = projects.get(target_project_id)
        if project is None:
            continue
        member = _project_member(runtime, target_project_id, target_agent_id)
        if member is None:
            continue
        qualified_id = format_agent_address(target_agent_id, target_project_id)
        targets[qualified_id] = SubAgentPromptTarget(
            agent_id=qualified_id,
            name=member.display_name,
            description=member.description,
        )
    return [targets[target_id] for target_id in sorted(targets)]


def _all_identity_scope_targets(
    runtime: RuntimeServices,
    caller_agent_id: str,
) -> list[SubAgentPromptTarget]:
    targets: dict[str, SubAgentPromptTarget] = {}
    for identity in runtime.agents.list():
        if identity.id == caller_agent_id:
            continue
        targets[identity.id] = SubAgentPromptTarget(agent_id=identity.id, name=identity.name)

    for project in runtime.projects.list():
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
            qualified_id = format_agent_address(member.agent_id, project.project_id)
            targets[qualified_id] = SubAgentPromptTarget(
                agent_id=qualified_id,
                name=member.display_name,
                description=member.description,
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


__all__ = ["SubAgentPromptTarget", "build_subagent_prompt_targets"]
