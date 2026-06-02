"""Agent-level tool availability helpers."""

from __future__ import annotations

from collections.abc import Sequence

from core.memory import MEMORY_PROMPT_MODE_OFF, MemoryPromptMode

MEMORY_TOOL_NAME = "memory"


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
) -> list[str] | None:
    """Return the runtime allowlist after applying Agent memory mode."""
    if allowed_tools is None:
        if memory_tool_enabled(memory_prompt_mode):
            return None
        return _without_memory(registered_tool_names)

    configured_tools = sanitize_configured_allowed_tools(allowed_tools)
    if "*" in configured_tools:
        if memory_tool_enabled(memory_prompt_mode):
            return configured_tools
        return _without_memory(registered_tool_names)

    if memory_tool_enabled(memory_prompt_mode):
        return [*configured_tools, MEMORY_TOOL_NAME]

    return configured_tools


def _without_memory(tool_names: Sequence[str]) -> list[str]:
    return sorted({tool_name for tool_name in tool_names if tool_name != MEMORY_TOOL_NAME})


__all__ = [
    "MEMORY_TOOL_NAME",
    "effective_agent_allowed_tools",
    "memory_tool_enabled",
    "sanitize_configured_allowed_tools",
]
