"""Which of an Agent's Tools its Tool list leaves to load on demand."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.tools import ToolAccess
from core.tools.on_demand import LOAD_TOOLS_TOOL_NAME, on_demand_tools

_TOOLS = (
    "apply_patch",
    "bash",
    "cron",
    "edit",
    "message_parent",
    "read",
    "web_search",
    "write",
    LOAD_TOOLS_TOOL_NAME,
)


def _agent(tool_loading: Any, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(
        tool_loading=tool_loading,
        builtin=fields.get("builtin"),
        tool_access=fields.get("tool_access", ToolAccess()),
    )


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        (_agent({"on_demand": True}), {"cron", "web_search"}),
        # Naming one file edit Tool keeps all of them.
        (
            _agent({"on_demand": True, "always_loaded": ["edit", "cron"]}),
            {"bash", "read", "web_search"},
        ),
        (
            _agent({"on_demand": True, "always_loaded": []}),
            {"apply_patch", "bash", "cron", "edit", "read", "web_search", "write"},
        ),
        (_agent({"on_demand": False, "always_loaded": []}), set()),
        (_agent(None), set()),
        (_agent({"on_demand": True}, builtin="librarian"), set()),
        (
            _agent(
                {"on_demand": True},
                tool_access=ToolAccess(mode="selected", allowed=("cron",), fixed=True),
            ),
            set(),
        ),
        # A temporary Agent has no setting.
        (SimpleNamespace(tool_access=ToolAccess()), set()),
    ],
    ids=[
        "default-set",
        "file-edit-unit",
        "none-always-loaded",
        "switched-off",
        "no-setting",
        "built-in-agent",
        "fixed-tool-set",
        "temporary-agent",
    ],
)
def test_on_demand_tools_leave_out_everything_but_the_listed_tools(
    agent: Any, expected: set[str]
) -> None:
    # Session-granted Tools and load_tools always stay in the Tool list.
    assert on_demand_tools(agent, _TOOLS, session_tool_grants=("message_parent",)) == expected
