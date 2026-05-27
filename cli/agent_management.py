"""Agent management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0
AGENT_UPDATE_FLAGS = (
    "--name",
    "--model",
    "--fallback-model",
    "--temperature",
    "--clear-temperature",
    "--thinking-effort",
    "--clear-thinking-effort",
    "--allowed-tools",
    "--allowed-skills",
    "--current-session-id",
)


def agent_list(instance: ServerInstance) -> CommandResult:
    """Return formatted agent list output from `agent.list` RPC."""

    payload = _rpc_call(instance, "agent.list", {})
    if not payload.ok:
        return payload.to_command_result()
    agents = payload.data.get("agents")
    if not isinstance(agents, list):
        return CommandResult(ok=False, message="RPC result missing agents list", instance=instance)
    return CommandResult(ok=True, message=_format_agent_rows(agents), instance=instance)


def agent_show(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Return one formatted agent from `agent.get` RPC."""

    payload = _rpc_call(instance, "agent.get", {"id": agent_id})
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(ok=True, message=_format_agent_detail(payload.data), instance=instance)


def agent_create(
    instance: ServerInstance,
    agent_id: str,
    name: str,
    changes: Mapping[str, Any],
) -> CommandResult:
    """Create an agent via `agent.create` RPC."""

    params = {"id": agent_id, "name": name, **dict(changes)}
    payload = _rpc_call(instance, "agent.create", params)
    if not payload.ok:
        return payload.to_command_result()
    created_id = _string_or_default(payload.data.get("id"), agent_id)
    return CommandResult(ok=True, message=f"created {created_id}", instance=instance)


def agent_update(
    instance: ServerInstance,
    agent_id: str,
    changes: Mapping[str, Any],
) -> CommandResult:
    """Update an agent via `agent.update` RPC."""

    if not changes:
        return CommandResult(
            ok=False,
            message=f"no agent fields provided; use one of: {', '.join(AGENT_UPDATE_FLAGS)}",
            instance=instance,
        )
    params = {"id": agent_id, **dict(changes)}
    payload = _rpc_call(instance, "agent.update", params)
    if not payload.ok:
        return payload.to_command_result()
    updated_id = _string_or_default(payload.data.get("id"), agent_id)
    return CommandResult(ok=True, message=f"updated {updated_id}", instance=instance)


def agent_delete(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Delete an agent via `agent.delete` RPC."""

    payload = _rpc_call(instance, "agent.delete", {"id": agent_id})
    if not payload.ok:
        return payload.to_command_result()
    deleted_id = _string_or_default(payload.data.get("agent_id"), agent_id)
    return CommandResult(ok=True, message=f"deleted {deleted_id}", instance=instance)


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
    """Call one server RPC method and return normalized success/error payload."""

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
    """Format a stable error message from server RPC error payload."""

    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if isinstance(code, str) and isinstance(message, str):
            return f"{code}: {message}"
        if isinstance(message, str):
            return message
    return fallback


def _format_agent_rows(agents: Sequence[object]) -> str:
    if not agents:
        return "no agents configured"

    lines = ["agents:"]
    for agent in agents:
        lines.append(_format_agent_row(agent))
    return "\n".join(lines)


def _format_agent_row(agent: object) -> str:
    if not isinstance(agent, dict):
        return "- invalid agent entry"

    agent_id = _string_or_default(agent.get("id"), "?")
    name = _string_or_default(agent.get("name"), "?")
    model = _string_or_default(agent.get("model"), "-")
    fallback_model = _string_or_default(agent.get("fallback_model"), "-")
    temperature = _value_text(agent.get("temperature"))
    thinking_effort = _value_text(agent.get("thinking_effort"))
    current_session_id = _string_or_default(agent.get("current_session_id"), "-")
    context_window = _value_text(agent.get("context_window"))
    return (
        f"- id={agent_id}"
        f" name={name}"
        f" model={model}"
        f" fallback_model={fallback_model}"
        f" temperature={temperature}"
        f" thinking_effort={thinking_effort}"
        f" current_session_id={current_session_id}"
        f" context_window={context_window}"
    )


def _format_agent_detail(agent: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "agent:",
            f"id: {_string_or_default(agent.get('id'), '?')}",
            f"name: {_string_or_default(agent.get('name'), '?')}",
            f"model: {_string_or_default(agent.get('model'), '-')}",
            f"fallback_model: {_string_or_default(agent.get('fallback_model'), '-')}",
            f"workspace: {_string_or_default(agent.get('workspace'), '-')}",
            f"temperature: {_value_text(agent.get('temperature'))}",
            f"thinking_effort: {_value_text(agent.get('thinking_effort'))}",
            f"allowed_tools: {_format_string_list(agent.get('allowed_tools'))}",
            f"allowed_skills: {_format_string_list(agent.get('allowed_skills'))}",
            f"current_session_id: {_string_or_default(agent.get('current_session_id'), '-')}",
            f"context_window: {_value_text(agent.get('context_window'))}",
            f"created_at: {_string_or_default(agent.get('created_at'), '-')}",
            f"updated_at: {_string_or_default(agent.get('updated_at'), '-')}",
        ]
    )


def _format_string_list(value: object) -> str:
    if not isinstance(value, list):
        return "-"
    if not value:
        return "[]"
    return ",".join(str(item) for item in value)


def _value_text(value: object) -> str:
    if value is None:
        return "-"
    return str(value)


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
