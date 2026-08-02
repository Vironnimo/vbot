"""Agent-level tool availability helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from core.memory import MEMORY_PROMPT_MODE_OFF, MemoryPromptMode

MEMORY_TOOL_NAME = "memory"
BASH_TOOL_SETTINGS_KEY = "bash"
BASH_ALLOWED_ENV_KEY = "allowed_env"
SKILL_MANAGE_TOOL_NAME = "skill_manage"
PROJECT_TOOL_NAME = "project"
SESSION_SEARCH_TOOL_NAME = "session_search"
SESSION_READ_TOOL_NAME = "session_read"
SUBAGENT_TOOL_NAMES: frozenset[str] = frozenset({"subagent"})
SUBAGENT_TOOL_SETTINGS_KEY = "subagent"
SUBAGENT_ALLOWED_AGENTS_KEY = "allowed_agents"
DEFAULT_SUBAGENT_ALLOWED_AGENTS: tuple[str, ...] = ("*",)
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Tools usable only by an identity agent (one with a Workspace). ``project`` loads
# context for work outside that Workspace, while ``skill_manage`` writes to the agent's
# private skill home. A config/project agent owns neither capability, so both are
# withheld even under a wildcard allow-list, like ``memory`` under its mode gate.
IDENTITY_ONLY_TOOLS: frozenset[str] = frozenset({PROJECT_TOOL_NAME, SKILL_MANAGE_TOOL_NAME})


def memory_tool_enabled(memory_prompt_mode: MemoryPromptMode) -> bool:
    """Return whether the memory tool should be callable for an Agent."""
    return memory_prompt_mode != MEMORY_PROMPT_MODE_OFF


def sanitize_configured_allowed_tools(allowed_tools: Sequence[str]) -> list[str]:
    """Return persisted/configurable tools without runtime-derived companions."""
    return [
        tool_name
        for tool_name in allowed_tools
        if tool_name not in {MEMORY_TOOL_NAME, SESSION_READ_TOOL_NAME}
    ]


def expand_companion_tools(allowed_tools: Sequence[str] | None) -> list[str] | None:
    """Derive companion Tools from their one persisted/configurable capability."""
    if allowed_tools is None:
        return None
    configured = [tool_name for tool_name in allowed_tools if tool_name != SESSION_READ_TOOL_NAME]
    expanded: list[str] = []
    for tool_name in configured:
        expanded.append(tool_name)
        if tool_name == SESSION_SEARCH_TOOL_NAME:
            expanded.append(SESSION_READ_TOOL_NAME)
    return list(dict.fromkeys(expanded))


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

    configured_tools = expand_companion_tools(
        [
            tool_name
            for tool_name in sanitize_configured_allowed_tools(allowed_tools)
            if tool_name not in excluded
        ]
    )
    assert configured_tools is not None
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
            _set_named_property_enum(parameters, "agent_id", targets)
        projected.append(narrowed)
    return projected


def _set_named_property_enum(schema: Any, property_name: str, values: list[str]) -> None:
    """Apply an enum to every matching property in one nested JSON Schema."""

    if isinstance(schema, list):
        for item in schema:
            _set_named_property_enum(item, property_name, values)
        return
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        selected = properties.get(property_name)
        if isinstance(selected, dict):
            selected["enum"] = list(values)
        for property_schema in properties.values():
            _set_named_property_enum(property_schema, property_name, values)
    for keyword in ("oneOf", "anyOf", "allOf"):
        _set_named_property_enum(schema.get(keyword), property_name, values)
    _set_named_property_enum(schema.get("items"), property_name, values)


def agent_tool_settings(agent_tools: Any) -> dict[str, Any]:
    """Copy the generic root ``tools`` mapping from one RuntimeAgent."""
    if not isinstance(agent_tools, Mapping):
        return {}
    return deepcopy(dict(agent_tools))


def normalize_env_keys(value: Any, *, field_name: str) -> list[str]:
    """Validate and deduplicate one ordered environment-key list."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of environment key names")
    invalid = [item for item in value if ENV_KEY_PATTERN.fullmatch(item) is None]
    if invalid:
        names = ", ".join(repr(item) for item in invalid)
        raise ValueError(f"{field_name} has invalid environment key name(s): {names}")
    return list(dict.fromkeys(value))


def bash_allowed_env_keys(tool_settings: Mapping[str, Any] | None) -> list[str]:
    """Return an Agent's validated permanent Bash environment grants."""
    if not isinstance(tool_settings, Mapping):
        return []
    bash = tool_settings.get(BASH_TOOL_SETTINGS_KEY)
    if not isinstance(bash, Mapping):
        return []
    allowed = bash.get(BASH_ALLOWED_ENV_KEY)
    if allowed is None:
        return []
    try:
        return normalize_env_keys(
            allowed,
            field_name=f"tools.{BASH_TOOL_SETTINGS_KEY}.{BASH_ALLOWED_ENV_KEY}",
        )
    except ValueError:
        return []


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
    "BASH_ALLOWED_ENV_KEY",
    "BASH_TOOL_SETTINGS_KEY",
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
    "bash_allowed_env_keys",
    "effective_agent_allowed_tools",
    "expand_companion_tools",
    "memory_tool_enabled",
    "normalize_env_keys",
    "sanitize_configured_allowed_tools",
    "subagent_allowed_agents",
]
