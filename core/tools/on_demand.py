"""On-demand Tools: the Tools an Agent's Tool list leaves out until the Agent loads them.

An Agent's ``tool_loading`` setting switches On-demand Tools on. Its Tool list
then holds only the always-loaded Tools; every other Tool the Agent may use is
listed by name and summary in its System Prompt, and the built-in ``load_tools``
Tool returns a Tool's definition when the Agent needs it. The setting changes
what the Model is shown, never which Tools the Agent may call.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from core.tools.availability import ToolAccess, normalize_tool_name_list
from core.tools.edit import edit_tool_siblings

LOAD_TOOLS_TOOL_NAME = "load_tools"

TOOL_LOADING_FIELDS = frozenset({"on_demand", "always_loaded"})

# The Tools an Agent keeps in its Tool list while ``always_loaded`` is not set.
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

    ``on_demand`` is required. ``always_loaded`` stays absent when the object
    leaves it out (the default set applies); a given list, also an empty one,
    is kept as given. Names of Tools that are not registered are kept, like
    Extension Tool names in a Tool policy. Raises ``ValueError``.
    """

    if not isinstance(value, Mapping):
        raise ValueError("tool_loading must be an object")
    unsupported = sorted(str(key) for key in set(value) - TOOL_LOADING_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported tool_loading fields: {', '.join(unsupported)}")
    if "on_demand" not in value:
        raise ValueError("tool_loading.on_demand is required")
    on_demand = value["on_demand"]
    if not isinstance(on_demand, bool):
        raise ValueError("tool_loading.on_demand must be a boolean")
    normalized: dict[str, Any] = {"on_demand": on_demand}
    if "always_loaded" in value:
        always_loaded = value["always_loaded"]
        if isinstance(always_loaded, list) and "*" in always_loaded:
            raise ValueError("tool_loading.always_loaded cannot contain '*'; name each Tool")
        normalized["always_loaded"] = list(
            normalize_tool_name_list(always_loaded, "tool_loading.always_loaded")
        )
    return normalized


def loads_tools_on_demand(agent: Any) -> bool:
    """Whether *agent* gets On-demand Tools.

    Only an Agent whose ``tool_loading`` switch is on does. A built-in Agent and
    an Agent with a fixed Tool set (``ToolAccess.fixed``) never do, whatever
    their configuration says; an Agent without the setting (a temporary Agent)
    does not either.
    """

    tool_loading = getattr(agent, "tool_loading", None)
    if not isinstance(tool_loading, Mapping) or tool_loading.get("on_demand") is not True:
        return False
    if getattr(agent, "builtin", None) is not None:
        return False
    tool_access = getattr(agent, "tool_access", None)
    return not (isinstance(tool_access, ToolAccess) and tool_access.fixed)


def always_loaded_tools(tool_loading: Mapping[str, Any] | None) -> frozenset[str]:
    """Return the Tools *tool_loading* keeps in the Tool list.

    The default set applies while ``always_loaded`` is absent. The file edit
    Tools count as one: naming one of them keeps all of them.
    """

    names: Iterable[str] = DEFAULT_ALWAYS_LOADED_TOOLS
    if isinstance(tool_loading, Mapping) and isinstance(tool_loading.get("always_loaded"), list):
        names = tool_loading["always_loaded"]
    selected = set(names)
    return frozenset({*selected, *edit_tool_siblings(selected)})


def on_demand_tools(
    agent: Any,
    tool_names: Iterable[str],
    *,
    session_tool_grants: Collection[str] = (),
) -> frozenset[str]:
    """Return which of *tool_names* the Tool list of *agent* leaves to load on demand.

    *tool_names* are registry names of the Tools *agent* may use. The Tool list
    keeps every Tool when *agent* does not load Tools on demand
    (:func:`loads_tools_on_demand`), and always keeps ``load_tools``, the Tools
    the Session grants (*session_tool_grants*) and the always-loaded Tools.
    """

    if not loads_tools_on_demand(agent):
        return frozenset()
    kept = always_loaded_tools(agent.tool_loading)
    grants = set(session_tool_grants)
    return frozenset(
        name
        for name in tool_names
        if name != LOAD_TOOLS_TOOL_NAME and name not in kept and name not in grants
    )


__all__ = [
    "DEFAULT_ALWAYS_LOADED_TOOLS",
    "LOAD_TOOLS_TOOL_NAME",
    "TOOL_LOADING_FIELDS",
    "always_loaded_tools",
    "loads_tools_on_demand",
    "normalize_tool_loading",
    "on_demand_tools",
]
