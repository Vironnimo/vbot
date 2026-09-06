"""Agent-level tool availability helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
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

TOOL_ACCESS_MODE_ALL = "all"
TOOL_ACCESS_MODE_SELECTED = "selected"
TOOL_ACCESS_MODE_NONE = "none"
TOOL_ACCESS_MODES: frozenset[str] = frozenset(
    {TOOL_ACCESS_MODE_ALL, TOOL_ACCESS_MODE_SELECTED, TOOL_ACCESS_MODE_NONE}
)

TOOL_ACTIVATION_CONFIGURABLE = "configurable"
TOOL_ACTIVATION_FOLLOWS = "follows"
TOOL_ACTIVATION_MEMORY_MODE = "memory_mode"
TOOL_ACTIVATION_SESSION_GRANT = "session_grant"
TOOL_ACTIVATION_KINDS: frozenset[str] = frozenset(
    {
        TOOL_ACTIVATION_CONFIGURABLE,
        TOOL_ACTIVATION_FOLLOWS,
        TOOL_ACTIVATION_MEMORY_MODE,
        TOOL_ACTIVATION_SESSION_GRANT,
    }
)

TOOL_CONSTRAINT_IDENTITY_AGENT = "identity_agent"
TOOL_CONSTRAINT_IMAGE_FALLBACK_ROUTE = "image_fallback_route"


@dataclass(frozen=True, slots=True)
class ToolAccess:
    """One Agent's explicit Tool policy, independent of runtime availability."""

    mode: str = TOOL_ACCESS_MODE_ALL
    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    granted: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical persisted/public JSON representation."""

        result: dict[str, Any] = {"mode": self.mode}
        if self.mode == TOOL_ACCESS_MODE_SELECTED:
            result["allowed"] = list(self.allowed)
        if self.denied:
            result["denied"] = list(self.denied)
        if self.granted:
            result["granted"] = list(self.granted)
        return result


@dataclass(frozen=True, slots=True)
class ToolAccessResolution:
    """Effective Tool names and the Session Grants that survived policy."""

    allowed_tools: tuple[str, ...]
    session_tool_grants: tuple[str, ...]


def memory_tool_enabled(memory_prompt_mode: MemoryPromptMode) -> bool:
    """Return whether the memory tool should be callable for an Agent."""
    return memory_prompt_mode != MEMORY_PROMPT_MODE_OFF


def normalize_tool_access(value: ToolAccess | Mapping[str, Any] | None) -> ToolAccess:
    """Validate and normalize one Tool access policy.

    A missing policy is the product default (all configurable Tools). A supplied
    mapping is strict: ``selected`` requires ``allowed`` while the other modes
    reject it. Denials may be retained in any mode because they are an absolute
    cross-mode preference, even though ``none`` already disables every Tool.
    Wildcards are retired from Agent Tool access entirely.
    """

    if value is None:
        return ToolAccess()
    if isinstance(value, ToolAccess):
        value = {
            "mode": value.mode,
            **({"allowed": list(value.allowed)} if value.mode == TOOL_ACCESS_MODE_SELECTED else {}),
            **({"denied": list(value.denied)} if value.denied else {}),
            **({"granted": list(value.granted)} if value.granted else {}),
        }
    if not isinstance(value, Mapping):
        raise ValueError("tool_access must be an object")

    unsupported = sorted(set(value) - {"mode", "allowed", "denied", "granted"})
    if unsupported:
        raise ValueError(f"unsupported tool_access fields: {', '.join(unsupported)}")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in TOOL_ACCESS_MODES:
        raise ValueError("tool_access.mode must be one of: all, selected, none")

    has_allowed = "allowed" in value
    if mode == TOOL_ACCESS_MODE_SELECTED and not has_allowed:
        raise ValueError("tool_access.allowed is required when mode is selected")
    if mode != TOOL_ACCESS_MODE_SELECTED and has_allowed:
        raise ValueError("tool_access.allowed is only valid when mode is selected")

    allowed = _normalize_tool_name_list(value.get("allowed", ()), "tool_access.allowed")
    denied = _normalize_tool_name_list(value.get("denied", ()), "tool_access.denied")
    granted = _normalize_tool_name_list(value.get("granted", ()), "tool_access.granted")
    overlap = sorted(set(allowed) & set(denied))
    if overlap:
        names = ", ".join(overlap)
        raise ValueError(f"tool_access.allowed and tool_access.denied overlap: {names}")
    return ToolAccess(mode=mode, allowed=allowed, denied=denied, granted=granted)


def resolve_tool_access(
    tool_access: ToolAccess,
    tools: Sequence[Any],
    memory_prompt_mode: MemoryPromptMode,
    *,
    workspace: str = "",
    session_tool_grants: Sequence[str] = (),
) -> ToolAccessResolution:
    """Resolve policy, automatic activation, constraints, grants, and denials once."""

    if tool_access.mode == TOOL_ACCESS_MODE_NONE:
        return ToolAccessResolution(allowed_tools=(), session_tool_grants=())

    catalog = {tool.name: tool for tool in tools if not getattr(tool, "internal", False)}
    if tool_access.mode == TOOL_ACCESS_MODE_ALL:
        active = {
            name
            for name, tool in catalog.items()
            if _activation_kind(tool) == TOOL_ACTIVATION_CONFIGURABLE
            and _constraints_allow(tool, workspace=workspace)
        }
    else:
        active = {
            name
            for name in tool_access.allowed
            if name in catalog
            and _activation_kind(catalog[name]) == TOOL_ACTIVATION_CONFIGURABLE
            and _constraints_allow(catalog[name], workspace=workspace)
        }

    # A whitelist (including a materialized Project ceiling) is never an opt-in.
    active = {
        name
        for name in active
        if not getattr(catalog[name], "requires_opt_in", False) or name in tool_access.granted
    }
    requested_grants = set(session_tool_grants)
    for name, tool in catalog.items():
        activation = _activation_kind(tool)
        if not _constraints_allow(tool, workspace=workspace):
            continue
        memory_activated = activation == TOOL_ACTIVATION_MEMORY_MODE and memory_tool_enabled(
            memory_prompt_mode
        )
        session_granted = activation == TOOL_ACTIVATION_SESSION_GRANT and name in requested_grants
        if memory_activated or session_granted:
            active.add(name)

    _add_followed_tools(active, catalog, workspace=workspace)
    active.difference_update(tool_access.denied)
    _remove_orphaned_followers(active, catalog)

    ordered_active = tuple(tool.name for tool in tools if tool.name in active)
    effective_grants = tuple(
        name
        for name in session_tool_grants
        if name in active
        and name in catalog
        and _activation_kind(catalog[name]) == TOOL_ACTIVATION_SESSION_GRANT
    )
    return ToolAccessResolution(
        allowed_tools=ordered_active,
        session_tool_grants=tuple(dict.fromkeys(effective_grants)),
    )


def _normalize_tool_name_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    names = tuple(dict.fromkeys(value))
    if len(names) != len(value):
        raise ValueError(f"{field_name} must not contain duplicate names")
    if any(not name.strip() for name in names):
        raise ValueError(f"{field_name} must not contain empty names")
    if "*" in names:
        raise ValueError(f"{field_name} cannot contain the retired wildcard '*'")
    return names


def _activation_kind(tool: Any) -> str:
    return str(getattr(tool, "activation", TOOL_ACTIVATION_CONFIGURABLE))


def _constraints_allow(tool: Any, *, workspace: str) -> bool:
    constraints = tuple(getattr(tool, "constraints", ()))
    return TOOL_CONSTRAINT_IDENTITY_AGENT not in constraints or bool(workspace)


def _add_followed_tools(active: set[str], catalog: Mapping[str, Any], *, workspace: str) -> None:
    changed = True
    while changed:
        changed = False
        for name, tool in catalog.items():
            if name in active or _activation_kind(tool) != TOOL_ACTIVATION_FOLLOWS:
                continue
            source = getattr(tool, "activation_source", None)
            if source in active and _constraints_allow(tool, workspace=workspace):
                active.add(name)
                changed = True


def _remove_orphaned_followers(active: set[str], catalog: Mapping[str, Any]) -> None:
    changed = True
    while changed:
        changed = False
        for name in tuple(active):
            tool = catalog[name]
            if _activation_kind(tool) != TOOL_ACTIVATION_FOLLOWS:
                continue
            if getattr(tool, "activation_source", None) not in active:
                active.remove(name)
                changed = True


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


__all__ = [
    "BASH_ALLOWED_ENV_KEY",
    "BASH_TOOL_SETTINGS_KEY",
    "DEFAULT_SUBAGENT_ALLOWED_AGENTS",
    "MEMORY_TOOL_NAME",
    "PROJECT_TOOL_NAME",
    "SESSION_READ_TOOL_NAME",
    "SESSION_SEARCH_TOOL_NAME",
    "SKILL_MANAGE_TOOL_NAME",
    "SUBAGENT_ALLOWED_AGENTS_KEY",
    "SUBAGENT_TOOL_SETTINGS_KEY",
    "SUBAGENT_TOOL_NAMES",
    "TOOL_ACCESS_MODE_ALL",
    "TOOL_ACCESS_MODE_NONE",
    "TOOL_ACCESS_MODE_SELECTED",
    "TOOL_ACCESS_MODES",
    "TOOL_ACTIVATION_CONFIGURABLE",
    "TOOL_ACTIVATION_FOLLOWS",
    "TOOL_ACTIVATION_KINDS",
    "TOOL_ACTIVATION_MEMORY_MODE",
    "TOOL_ACTIVATION_SESSION_GRANT",
    "TOOL_CONSTRAINT_IDENTITY_AGENT",
    "TOOL_CONSTRAINT_IMAGE_FALLBACK_ROUTE",
    "ToolAccess",
    "ToolAccessResolution",
    "agent_tool_settings",
    "apply_agent_target_tool_visibility",
    "bash_allowed_env_keys",
    "memory_tool_enabled",
    "normalize_env_keys",
    "normalize_tool_access",
    "resolve_tool_access",
    "subagent_allowed_agents",
]
