"""The ``tool_loading`` setting: its shape, validation and inheritance.

``tool_loading`` switches On-demand Tools (``core.tools.on_demand``) and names the
Tools that stay in the Tool list. The global default ``defaults.agent.tool_loading``,
an Identity Agent's own setting and a Project Agent override share this shape; each
of its two parts, ``on_demand`` and ``always_loaded``, an Agent either sets itself
or inherits from the global default.

The module is a leaf, so Settings can validate the global default without
importing the Tool stack.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

TOOL_LOADING_FIELDS = frozenset({"on_demand", "always_loaded"})

# The Tools an Agent keeps in its Tool list while neither it nor the global
# default names its always-loaded Tools.
DEFAULT_ALWAYS_LOADED_TOOLS: tuple[str, ...] = (
    "read",
    "write",
    "edit",
    "apply_patch",
    "bash",
    "search_files",
    "skill",
    "memory",
    "subagent",
    "project",
)


def normalize_tool_loading(value: Any) -> dict[str, Any]:
    """Validate one ``tool_loading`` object and return its canonical copy.

    Both parts are optional: a part the object leaves out is inherited (an empty
    object sets nothing). A given ``always_loaded`` list, also an empty one, is
    kept as given, including names of Tools that are not registered, like
    Extension Tool names in a Tool policy. Raises ``ValueError``.
    """

    from core.tools.availability import normalize_tool_name_list

    if not isinstance(value, Mapping):
        raise ValueError("tool_loading must be an object")
    unsupported = sorted(str(key) for key in set(value) - TOOL_LOADING_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported tool_loading fields: {', '.join(unsupported)}")
    normalized: dict[str, Any] = {}
    if "on_demand" in value:
        on_demand = value["on_demand"]
        if not isinstance(on_demand, bool):
            raise ValueError("tool_loading.on_demand must be a boolean")
        normalized["on_demand"] = on_demand
    if "always_loaded" in value:
        always_loaded = value["always_loaded"]
        if isinstance(always_loaded, list) and "*" in always_loaded:
            raise ValueError("tool_loading.always_loaded cannot contain '*'; name each Tool")
        normalized["always_loaded"] = list(
            normalize_tool_name_list(always_loaded, "tool_loading.always_loaded")
        )
    return normalized


def inherit_tool_loading(
    own: Mapping[str, Any] | None, default: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Return the ``tool_loading`` an Agent runs with: each part its own, else the default's.

    ``None`` when neither sets a part: the Agent has no On-demand Tools.
    """

    inherited: dict[str, Any] = {}
    for field in ("on_demand", "always_loaded"):
        for source in (own, default):
            if isinstance(source, Mapping) and field in source:
                inherited[field] = copy.deepcopy(source[field])
                break
    return inherited or None


def complete_tool_loading(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return *value* with both parts filled: switched off, the built-in always-loaded Tools."""

    source = value if isinstance(value, Mapping) else {}
    always_loaded = source.get("always_loaded")
    return {
        "on_demand": source.get("on_demand") is True,
        "always_loaded": (
            list(always_loaded)
            if isinstance(always_loaded, list)
            else list(DEFAULT_ALWAYS_LOADED_TOOLS)
        ),
    }


__all__ = [
    "DEFAULT_ALWAYS_LOADED_TOOLS",
    "TOOL_LOADING_FIELDS",
    "complete_tool_loading",
    "inherit_tool_loading",
    "normalize_tool_loading",
]
