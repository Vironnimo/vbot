"""Provider management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import get_close_matches
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


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
            ok=False,
            message=_format_status_not_found(target, connection_id, connections),
            instance=instance,
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


def _format_status_not_found(
    target: str,
    connection_id: str | None,
    connections: Sequence[object],
) -> str:
    candidates = (
        _connection_ids(connections) if connection_id is not None else _provider_ids(connections)
    )
    lines = [f"provider status not found: {target}"]
    if candidates:
        label = "connections" if connection_id is not None else "providers"
        lines.append(f"available {label}: {', '.join(candidates)}")
        suggestions = get_close_matches(target, candidates, n=1)
        if suggestions:
            lines.append(f"did you mean: {suggestions[0]}")
    return "\n".join(lines)


def _provider_ids(connections: Sequence[object]) -> list[str]:
    provider_ids: set[str] = set()
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        provider_id = connection.get("provider_id")
        if isinstance(provider_id, str):
            provider_ids.add(provider_id)
    return sorted(provider_ids)


def _connection_ids(connections: Sequence[object]) -> list[str]:
    connection_ids: set[str] = set()
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        connection_id = connection.get("id")
        if isinstance(connection_id, str):
            connection_ids.add(connection_id)
    return sorted(connection_ids)


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
