"""Member Tool ceilings owned by Channels, independent of Agent permissions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.channels.config import ChannelConfigError
from core.tools.availability import ToolAccess, activation_sources, normalize_tool_access

DEFAULT_MEMBER_TOOLS = ToolAccess(mode="selected", allowed=("web_search", "web_fetch"))


def normalize_member_tools(value: Any) -> ToolAccess:
    """Read an explicit ceiling; absence/inheritance is handled by the caller."""
    if not isinstance(value, dict) or set(value) - {"mode", "allowed"}:
        raise ChannelConfigError("member_tools must contain only mode and allowed")
    try:
        return normalize_tool_access(value)
    except ValueError as error:
        raise ChannelConfigError(str(error)) from error


def member_tool_names(policy: ToolAccess, catalog: Sequence[Any] = ()) -> tuple[str, ...] | None:
    """Expand selected capabilities to their followers, without granting Agent access."""
    if policy.mode == "all":
        return None
    if policy.mode == "none":
        return ()
    names = set(policy.allowed)
    # Followers can themselves have followers (including an MCP connection's
    # remote Tools). This only expands the ceiling; Agent policy still decides
    # whether each Tool, including an opt-in Tool, is actually allowed.
    while True:
        added = {
            tool.name
            for tool in catalog
            if tool.activation == "follows" and names.intersection(activation_sources(tool))
        } - names
        if not added:
            break
        names.update(added)
    if names:
        # Discovery grants nothing: load_tools filters its results by this Run's
        # ceiling and live denial resolver.
        names.add("load_tools")
    return tuple(sorted(names))
