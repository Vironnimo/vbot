"""Provider management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0


def provider_list(instance: ServerInstance) -> CommandResult:
    """Return formatted provider connection output from `connection.list` RPC."""

    payload = _rpc_call(instance, "connection.list", {})
    if not payload.ok:
        return payload.to_command_result()
    connections = payload.data.get("connections")
    if not isinstance(connections, list):
        return CommandResult(
            ok=False,
            message="RPC result missing connections list",
            instance=instance,
        )
    return CommandResult(ok=True, message=_format_connection_rows(connections), instance=instance)


def provider_status(
    instance: ServerInstance,
    provider_id: str,
    connection_id: str | None = None,
) -> CommandResult:
    """Return filtered provider connection status from `connection.list` RPC."""

    payload = _rpc_call(instance, "connection.list", {})
    if not payload.ok:
        return payload.to_command_result()
    connections = payload.data.get("connections")
    if not isinstance(connections, list):
        return CommandResult(
            ok=False,
            message="RPC result missing connections list",
            instance=instance,
        )

    filtered_connections = _filter_connections(connections, provider_id, connection_id)
    if not filtered_connections:
        target = connection_id if connection_id is not None else provider_id
        return CommandResult(
            ok=False, message=f"provider status not found: {target}", instance=instance
        )
    return CommandResult(
        ok=True,
        message=_format_connection_rows(filtered_connections),
        instance=instance,
    )


def provider_set_key(
    instance: ServerInstance,
    provider_id: str,
    value: str,
    connection_id: str | None = None,
    refresh_models: bool = False,
) -> CommandResult:
    """Set an API-key provider credential via `provider.set_key` RPC."""

    params: dict[str, Any] = {"provider_id": provider_id, "value": value}
    if connection_id is not None:
        params["connection_id"] = connection_id

    payload = _rpc_call(instance, "provider.set_key", params)
    if not payload.ok:
        return payload.to_command_result()

    resolved_connection_id = _string_or_default(payload.data.get("connection_id"), "?")
    credential_key = _string_or_default(payload.data.get("credential_key"), "?")
    message = f"set {resolved_connection_id} credential {credential_key}"

    if refresh_models:
        refresh_payload = _rpc_call(instance, "model.refresh_db", {"provider_id": provider_id})
        if not refresh_payload.ok:
            return CommandResult(
                ok=False,
                message=f"{message}\nrefresh failed: {refresh_payload.message}",
                instance=instance,
            )
        message = f"{message}\n{_format_refresh_result(refresh_payload.data, provider_id)}"

    return CommandResult(
        ok=True,
        message=message,
        instance=instance,
    )


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


def _format_connection_rows(connections: Sequence[object]) -> str:
    if not connections:
        return "no connections configured"

    lines = ["connections:"]
    for connection in connections:
        lines.append(_format_connection_row(connection))
    return "\n".join(lines)


def _filter_connections(
    connections: Sequence[object],
    provider_id: str,
    connection_id: str | None,
) -> list[object]:
    filtered_connections: list[object] = []
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        if connection.get("provider_id") != provider_id:
            continue
        if connection_id is not None and connection.get("id") != connection_id:
            continue
        filtered_connections.append(connection)
    return filtered_connections


def _format_connection_row(connection: object) -> str:
    if not isinstance(connection, dict):
        return "- invalid connection entry"

    connection_id = _string_or_default(connection.get("id"), "?")
    provider_id = _string_or_default(connection.get("provider_id"), "?")
    connection_type = _string_or_default(connection.get("type"), "?")
    label = _string_or_default(connection.get("label"), "?")
    usable = "yes" if connection.get("usable") else "no"
    return (
        f"- id: {connection_id}"
        f"  provider_id: {provider_id}"
        f"  type: {connection_type}"
        f"  label: {label}"
        f"  usable: {usable}"
    )


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default


def _format_refresh_result(data: Mapping[str, Any], provider_id: str) -> str:
    resolved_provider_id = _string_or_default(data.get("provider_id"), provider_id)
    model_count = data.get("model_count")
    if isinstance(model_count, int) and not isinstance(model_count, bool):
        return f"refreshed {resolved_provider_id} ({model_count} models)"
    return f"refreshed {resolved_provider_id}"
