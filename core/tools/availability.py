"""Agent-level tool availability helpers."""

from __future__ import annotations

from collections.abc import Sequence

from core.memory import MEMORY_PROMPT_MODE_OFF, MemoryPromptMode

MEMORY_TOOL_NAME = "memory"
SKILL_MANAGE_TOOL_NAME = "skill_manage"

# Tools usable only by an identity agent (one with a Workspace). ``skill_manage``
# writes to the agent's own private skill home under ``<data_dir>/agents/<id>/skills/``,
# which a config/project agent (empty workspace) does not own — so it is withheld from
# those agents even under a wildcard allow-list, the same way ``memory`` is gated by the
# agent's memory mode.
IDENTITY_ONLY_TOOLS: frozenset[str] = frozenset({SKILL_MANAGE_TOOL_NAME})


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
) -> list[str] | None:
    """Return the runtime allowlist after applying Agent memory mode and identity-only gating.

    A config/project agent (empty ``workspace``) never gets an ``IDENTITY_ONLY_TOOLS``
    member (``skill_manage``) in its effective set, even under a wildcard allow-list —
    the same shape as the ``memory`` mode gate below. This is the dispatch-time
    allowlist ``ToolRegistry.dispatch`` actually enforces, so it must not grant more
    than what the prompt layer already advertises to the agent; the prompt-layer
    visibility pass alone (``_apply_identity_only_tool_visibility``) only hides the
    tool definition from the model, it does not block a call that reaches dispatch.
    """
    excluded: set[str] = set() if workspace else set(IDENTITY_ONLY_TOOLS)
    if not memory_tool_enabled(memory_prompt_mode):
        excluded.add(MEMORY_TOOL_NAME)

    if allowed_tools is None:
        return None if not excluded else _without(registered_tool_names, excluded)

    configured_tools = [
        tool_name
        for tool_name in sanitize_configured_allowed_tools(allowed_tools)
        if tool_name not in excluded
    ]
    if "*" in configured_tools:
        return configured_tools if not excluded else _without(registered_tool_names, excluded)

    if memory_tool_enabled(memory_prompt_mode):
        return [*configured_tools, MEMORY_TOOL_NAME]

    return configured_tools


def _without(tool_names: Sequence[str], excluded: set[str]) -> list[str]:
    return sorted({tool_name for tool_name in tool_names if tool_name not in excluded})


__all__ = [
    "IDENTITY_ONLY_TOOLS",
    "MEMORY_TOOL_NAME",
    "SKILL_MANAGE_TOOL_NAME",
    "effective_agent_allowed_tools",
    "memory_tool_enabled",
    "sanitize_configured_allowed_tools",
]
