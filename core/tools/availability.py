"""Agent-level tool availability helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from core.memory import MEMORY_PROMPT_MODE_OFF, MemoryPromptMode

MEMORY_TOOL_NAME = "memory"
SKILL_MANAGE_TOOL_NAME = "skill_manage"
PROJECT_TOOL_NAME = "project"
SUBAGENT_TOOL_NAMES: frozenset[str] = frozenset({"subagent", "subagent_result"})
SUBAGENT_TOOL_SETTINGS_KEY = "subagent"
SUBAGENT_ALLOWED_AGENTS_KEY = "allowed_agents"
DEFAULT_SUBAGENT_ALLOWED_AGENTS: tuple[str, ...] = ("*",)

# Tools usable only by an identity agent (one with a Workspace). ``project`` loads
# context for work outside that Workspace, while ``skill_manage`` writes to the agent's
# private skill home. A config/project agent owns neither capability, so both are
# withheld even under a wildcard allow-list, like ``memory`` under its mode gate.
IDENTITY_ONLY_TOOLS: frozenset[str] = frozenset({PROJECT_TOOL_NAME, SKILL_MANAGE_TOOL_NAME})


def memory_tool_enabled(memory_prompt_mode: MemoryPromptMode) -> bool:
    """Return whether the memory tool should be callable for an Agent."""
    return memory_prompt_mode != MEMORY_PROMPT_MODE_OFF


def sanitize_configured_allowed_tools(allowed_tools: Sequence[str]) -> list[str]:
    """Return persisted/configurable tools without runtime-derived memory access."""
    return [tool_name for tool_name in allowed_tools if tool_name != MEMORY_TOOL_NAME]


def effective_agent_allowed_tools(
    allowed_tools: Sequence[str] | None,
    memory_prompt_mode: MemoryPromptMode,
    *,
    registered_tool_names: Sequence[str],
    workspace: str = "",
    session_tool_grants: Sequence[str] = (),
) -> list[str] | None:
    """Return the runtime allowlist after applying Agent memory mode and identity-only gating.

    A config/project agent (empty ``workspace``) never gets an ``IDENTITY_ONLY_TOOLS``
    member (``project`` or ``skill_manage``) in its effective set, even under a
    wildcard allow-list — the same shape as the ``memory`` mode gate below. This is
    the dispatch-time allowlist ``ToolRegistry.dispatch`` actually enforces, so it
    must not grant more than what the prompt layer already advertises to the agent;
    the prompt-layer
    visibility pass alone (``_apply_identity_only_tool_visibility``) only hides the
    tool definition from the model, it does not block a call that reaches dispatch.
    """
    excluded: set[str] = set() if workspace else set(IDENTITY_ONLY_TOOLS)
    if not memory_tool_enabled(memory_prompt_mode):
        excluded.add(MEMORY_TOOL_NAME)
    grants = [tool_name for tool_name in session_tool_grants if tool_name in registered_tool_names]

    if allowed_tools is None:
        if not excluded and not grants:
            return None
        return sorted(set(_without(registered_tool_names, excluded)) | set(grants))

    configured_tools = [
        tool_name
        for tool_name in sanitize_configured_allowed_tools(allowed_tools)
        if tool_name not in excluded
    ]
    if "*" in configured_tools:
        effective = configured_tools if not excluded else _without(registered_tool_names, excluded)
        if "*" in effective:
            return effective
        return sorted(set(effective) | set(grants))

    if memory_tool_enabled(memory_prompt_mode):
        return list(dict.fromkeys([*configured_tools, MEMORY_TOOL_NAME, *grants]))

    return list(dict.fromkeys([*configured_tools, *grants]))


def apply_agent_target_tool_visibility(
    definitions: list[dict[str, Any]],
    *,
    agent_id: str,
    allowed_agents: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Narrow Sub-Agent Tool schemas while keeping self-delegation implicit."""
    if allowed_agents is None or "*" in allowed_agents:
        return definitions

    targets = list(dict.fromkeys([agent_id, *allowed_agents]))
    projected: list[dict[str, Any]] = []
    for definition in definitions:
        if definition.get("name") not in SUBAGENT_TOOL_NAMES:
            projected.append(definition)
            continue
        narrowed = deepcopy(definition)
        parameters = narrowed.get("parameters")
        if isinstance(parameters, dict):
            properties = parameters.get("properties")
            if isinstance(properties, dict) and isinstance(properties.get("agent_id"), dict):
                properties["agent_id"]["enum"] = targets
        projected.append(narrowed)
    return projected


def agent_tool_settings(agent_tools: Any) -> dict[str, Any]:
    """Copy the generic root ``tools`` mapping from one RuntimeAgent."""
    if not isinstance(agent_tools, Mapping):
        return {}
    return deepcopy(dict(agent_tools))


def subagent_allowed_agents(tool_settings: Mapping[str, Any] | None) -> list[str]:
    """Resolve the Sub-Agent target policy from its optional Tool settings block."""
    if not isinstance(tool_settings, Mapping):
        return list(DEFAULT_SUBAGENT_ALLOWED_AGENTS)
    subagent = tool_settings.get(SUBAGENT_TOOL_SETTINGS_KEY)
    if subagent is None:
        return list(DEFAULT_SUBAGENT_ALLOWED_AGENTS)
    if not isinstance(subagent, Mapping):
        return []
    allowed = subagent.get(SUBAGENT_ALLOWED_AGENTS_KEY)
    if allowed is None:
        return list(DEFAULT_SUBAGENT_ALLOWED_AGENTS)
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        return []
    return list(allowed)


def _without(tool_names: Sequence[str], excluded: set[str]) -> list[str]:
    return sorted({tool_name for tool_name in tool_names if tool_name not in excluded})


__all__ = [
    "IDENTITY_ONLY_TOOLS",
    "MEMORY_TOOL_NAME",
    "PROJECT_TOOL_NAME",
    "SKILL_MANAGE_TOOL_NAME",
    "DEFAULT_SUBAGENT_ALLOWED_AGENTS",
    "SUBAGENT_ALLOWED_AGENTS_KEY",
    "SUBAGENT_TOOL_SETTINGS_KEY",
    "SUBAGENT_TOOL_NAMES",
    "agent_tool_settings",
    "apply_agent_target_tool_visibility",
    "effective_agent_allowed_tools",
    "memory_tool_enabled",
    "sanitize_configured_allowed_tools",
    "subagent_allowed_agents",
]
