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
    account: str | None = None,
) -> CommandResult:
    """Set an API-key provider credential via `provider.set_key` RPC."""

    params: dict[str, Any] = {"provider_id": provider_id, "value": value}
    if connection_id is not None:
        params["connection_id"] = connection_id
    if account is not None:
        params["account"] = account

    payload = _rpc_call(instance, "provider.set_key", params)
    if not payload.ok:
        return payload.to_command_result()

    resolved_connection_id = _string_or_default(payload.data.get("connection_id"), "?")
    credential_key = _string_or_default(payload.data.get("credential_key"), "?")
    resolved_account = _string_or_default(payload.data.get("account"), "default")
    message = (
        f"set {resolved_connection_id} credential {credential_key} (account: {resolved_account})"
    )

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


def provider_unset_key(
    instance: ServerInstance,
    provider_id: str,
    connection_id: str | None = None,
    account: str | None = None,
) -> CommandResult:
    """Remove an API-key provider credential via `provider.unset_key` RPC."""

    params: dict[str, Any] = {"provider_id": provider_id}
    if connection_id is not None:
        params["connection_id"] = connection_id
    if account is not None:
        params["account"] = account

    payload = _rpc_call(instance, "provider.unset_key", params)
    if not payload.ok:
        return payload.to_command_result()

    resolved_connection_id = _string_or_default(payload.data.get("connection_id"), "?")
    credential_key = _string_or_default(payload.data.get("credential_key"), "?")
    resolved_account = _string_or_default(payload.data.get("account"), "default")
    if not payload.data.get("removed"):
        message = (
            f"no stored credential {credential_key} for {resolved_connection_id} "
            f"(account: {resolved_account})"
        )
    else:
        message = (
            f"removed {resolved_connection_id} credential {credential_key} "
            f"(account: {resolved_account})"
        )
    if payload.data.get("configured"):
        message = (
            f"{message}\nstill configured from the process environment; "
            "unset the variable there to fully disable the connection"
        )
    return CommandResult(ok=True, message=message, instance=instance)


def provider_connect(
    instance: ServerInstance,
    provider_id: str,
    connection_id: str,
    account: str | None = None,
) -> CommandResult:
    """Start the OAuth device flow via `provider.connect` RPC."""

    params: dict[str, Any] = {"provider_id": provider_id, "connection_id": connection_id}
    if account is not None:
        params["account"] = account
    payload = _rpc_call(instance, "provider.connect", params)
    if not payload.ok:
        return payload.to_command_result()

    user_code = _string_or_default(payload.data.get("user_code"), "?")
    verification_uri = _string_or_default(payload.data.get("verification_uri"), "?")
    expires_in = payload.data.get("expires_in")
    expires_text = str(expires_in) if isinstance(expires_in, int) else "?"
    resolved_account = _string_or_default(payload.data.get("account"), "default")
    follow_up_command = f"provider connect-status {provider_id} --connection {connection_id}"
    if resolved_account != "default":
        follow_up_command = f"{follow_up_command} --account {resolved_account}"
    return CommandResult(
        ok=True,
        message="\n".join(
            [
                f"device flow started for {connection_id} (account: {resolved_account})",
                f"user_code: {user_code}",
                f"verification_uri: {verification_uri}",
                f"expires_in_seconds: {expires_text}",
                "enter the user code at the verification URI in a browser; then check "
                f"progress with: {follow_up_command}",
            ]
        ),
        instance=instance,
    )


def provider_disconnect(
    instance: ServerInstance,
    provider_id: str,
    connection_id: str,
    account: str | None = None,
) -> CommandResult:
    """Remove a stored OAuth token via `provider.disconnect` RPC."""

    params: dict[str, Any] = {"provider_id": provider_id, "connection_id": connection_id}
    if account is not None:
        params["account"] = account
    payload = _rpc_call(instance, "provider.disconnect", params)
    if not payload.ok:
        return payload.to_command_result()
    resolved_account = _string_or_default(payload.data.get("account"), "default")
    return CommandResult(
        ok=True,
        message=f"disconnected {connection_id} (account: {resolved_account})",
        instance=instance,
    )


def provider_connect_status(
    instance: ServerInstance,
    provider_id: str,
    connection_id: str,
    account: str | None = None,
) -> CommandResult:
    """Show OAuth connection state via `provider.connection_status` RPC."""

    params: dict[str, Any] = {"provider_id": provider_id, "connection_id": connection_id}
    if account is not None:
        params["account"] = account
    payload = _rpc_call(instance, "provider.connection_status", params)
    if not payload.ok:
        return payload.to_command_result()
    connected = "yes" if payload.data.get("connected") else "no"
    flow_active = "yes" if payload.data.get("flow_active") else "no"
    resolved_account = _string_or_default(payload.data.get("account"), "default")
    return CommandResult(
        ok=True,
        message=(
            f"{connection_id}: account={resolved_account} "
            f"connected={connected} flow_active={flow_active}"
        ),
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
    header = (
        f"- id: {connection_id}"
        f"  provider_id: {provider_id}"
        f"  type: {connection_type}"
        f"  label: {label}"
        f"  usable: {usable}"
    )
    return "\n".join([header, _format_account_rows(connection.get("accounts"))])


def _format_account_rows(accounts: object) -> str:
    if not isinstance(accounts, list) or not accounts:
        return "  accounts: none"

    lines = ["  accounts:"]
    for account in accounts:
        if not isinstance(account, dict):
            lines.append("  - invalid account entry")
            continue
        account_id = _string_or_default(account.get("id"), "?")
        usable = "yes" if account.get("usable") else "no"
        source = _string_or_default(account.get("source"), "?")
        lines.append(f"  - id: {account_id}  usable: {usable}  source: {source}")
    return "\n".join(lines)


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
