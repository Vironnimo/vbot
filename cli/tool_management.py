"""Tool catalog RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0


def tool_list(instance: ServerInstance) -> CommandResult:
    """Return formatted tool catalog output from `tool.list` RPC."""

    payload = _rpc_call(instance, "tool.list", {})
    if not payload.ok:
        return payload.to_command_result()
    tools = payload.data.get("tools")
    if not isinstance(tools, list):
        return CommandResult(ok=False, message="RPC result missing tools list", instance=instance)
    return CommandResult(ok=True, message=_format_tool_rows(tools), instance=instance)


class _RpcPayload:
    def __init__(
        self,
        *,
        ok: bool,
        instance: ServerInstance,
        data: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> None:
        self.ok = ok
        self.instance = instance
        self.data = data or {}
        self.message = message

    def to_command_result(self) -> CommandResult:
        return CommandResult(ok=False, message=self.message, instance=self.instance)


def _rpc_call(instance: ServerInstance, method: str, params: dict[str, Any]) -> _RpcPayload:
    request_body = {"method": method, "params": params}
    try:
        response = httpx.post(
            f"{instance.url}{RPC_PATH}",
            json=request_body,
            timeout=RPC_TIMEOUT_SECONDS,
        )
    except httpx.RequestError as exc:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=f"RPC request failed: {exc.__class__.__name__}",
        )

    try:
        payload = response.json()
    except ValueError:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=f"RPC response was not JSON (HTTP {response.status_code})",
        )

    if not isinstance(payload, dict):
        return _RpcPayload(ok=False, instance=instance, message="RPC response must be an object")

    if response.status_code != httpx.codes.OK:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=_rpc_error_message(
                payload.get("error"),
                fallback=f"RPC request failed with HTTP {response.status_code}",
            ),
        )

    ok_flag = payload.get("ok")
    if ok_flag is True:
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return _RpcPayload(ok=False, instance=instance, message="RPC result must be an object")
        return _RpcPayload(ok=True, instance=instance, data=result)
    if ok_flag is False:
        return _RpcPayload(
            ok=False,
            instance=instance,
            message=_rpc_error_message(payload.get("error"), fallback="RPC request failed"),
        )

    return _RpcPayload(ok=False, instance=instance, message="RPC response missing boolean ok flag")


def _rpc_error_message(error: object, *, fallback: str) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if isinstance(code, str) and isinstance(message, str):
            return f"{code}: {message}"
        if isinstance(message, str):
            return message
    return fallback


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
