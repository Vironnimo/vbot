"""Tool, skill, and command catalog RPC method registry."""

from __future__ import annotations

from typing import Any

from server.rpc.dispatcher import RpcMethodHandler


def method_handlers(delegates: Any) -> dict[str, RpcMethodHandler]:
    """Return read-only catalog RPC handlers from the delegates facade."""

    return {
        "tool.list": delegates._list_tools,
        "skill.list": delegates._list_skills,
        "chat.commands": delegates._list_commands,
    }
