"""Tool catalog RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Sequence

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def tool_list(instance: ServerInstance) -> CommandResult:
    """Return formatted tool catalog output from `tool.list` RPC."""

    payload = _rpc_call(instance, "tool.list", {})
    if not payload.ok:
        return payload.to_command_result()
    tools = payload.data.get("tools")
    if not isinstance(tools, list):
        return CommandResult(ok=False, message="RPC result missing tools list", instance=instance)
    return CommandResult(ok=True, message=_format_tool_rows(tools), instance=instance)


def _format_tool_rows(tools: Sequence[object]) -> str:
    if not tools:
        return "no tools configured"

    lines = ["tools:"]
    for tool in tools:
        lines.append(_format_tool_row(tool))
    return "\n".join(lines)


def _format_tool_row(tool: object) -> str:
    if not isinstance(tool, dict):
        return "- invalid tool entry"
    name = _string_or_default(tool.get("name"), "?")
    description = _string_or_default(tool.get("description"), "?")
    return f"- {name}  {description}"


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
