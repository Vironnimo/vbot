"""Tool implementation contracts shared with built-in Tools."""

from typing import Any

from ._exports import resolve

_EXPORTS = dict.fromkeys(
    ("Tool", "ToolContext", "ToolError", "tool_success", "tool_failure"), "core.tools.tools"
)


def __getattr__(name: str) -> Any:
    value = resolve(__name__, _EXPORTS, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = list(_EXPORTS)
