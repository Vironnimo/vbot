"""Provider management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import get_close_matches
from typing import Any

from cli._progress import Status, status_line
from cli.formatting import string_or_default as _string_or_default
from cli.formatting import value_text as _value_text
from cli.model_management import model_refresh
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def provider_list(instance: ServerInstance, *, details: bool = False) -> CommandResult:
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
    return CommandResult(
        ok=True, message=_format_connection_rows(connections, details=details), instance=instance
    )


def provider_custom_list(instance: ServerInstance) -> CommandResult:
    """List Settings-owned Custom Providers."""

    payload = _rpc_call(instance, "provider.custom_list", {})
    if not payload.ok:
        return payload.to_command_result()
    providers = payload.data.get("providers")
    if not isinstance(providers, list):
        return CommandResult(
            ok=False,
            message="RPC result missing providers list",
            instance=instance,
        )
    if not providers:
        return CommandResult(ok=True, message="no Custom Providers configured", instance=instance)
    lines = ["Custom Providers:"]
    for provider in providers:
        if not isinstance(provider, Mapping):
            continue
        provider_id = _string_or_default(provider.get("id"), "?")
        name = _string_or_default(provider.get("name"), provider_id)
        auth = _string_or_default(provider.get("auth"), "?")
        base_url = _string_or_default(provider.get("base_url"), "?")
        model_count = provider.get("model_count")
        count = model_count if isinstance(model_count, int) else "?"
        configured = "yes" if provider.get("credentials_configured") else "no"
        lines.append(
            f"- id: {provider_id}  name: {name}  auth: {auth}  "
            f"configured: {configured}  models: {count}  endpoint: {base_url}"
        )
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def provider_custom_save(
    instance: ServerInstance,
    provider_id: str,
    *,
    name: str,
    adapter: str,
    base_url: str,
    auth: str,
    api_key: str | None = None,
    models_endpoint: str | None = None,
    model_ids: Sequence[str] = (),
) -> CommandResult:
    """Create or replace one Custom Provider through RPC."""

    provider: dict[str, Any] = {
        "id": provider_id,
        "name": name,
        "adapter": adapter,
        "base_url": base_url,
        "auth": auth,
        "models": {model_id: {"name": model_id, "capabilities": {}} for model_id in model_ids},
    }
    if models_endpoint is not None:
        provider["models_endpoint"] = models_endpoint
    params: dict[str, Any] = {"provider": provider}
    if api_key is not None:
        params["api_key"] = api_key

    payload = _rpc_call(instance, "provider.custom_save", params)
    if not payload.ok:
        return payload.to_command_result()
    result = payload.data.get("provider")
    if not isinstance(result, Mapping):
        return CommandResult(
            ok=False,
            message="RPC result missing provider",
            instance=instance,
        )
    saved_id = _string_or_default(result.get("id"), provider_id)
    count = result.get("model_count")
    model_count = count if isinstance(count, int) else len(model_ids)
    usable = "usable" if result.get("usable") else "not usable"
    return CommandResult(
        ok=True,
        message=f"saved Custom Provider {saved_id} ({model_count} models, {usable})",
        instance=instance,
    )


def provider_custom_delete(
    instance: ServerInstance,
    provider_id: str,
) -> CommandResult:
    """Delete one Custom Provider through RPC."""

    payload = _rpc_call(
        instance,
        "provider.custom_delete",
        {"provider_id": provider_id},
    )
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=f"deleted Custom Provider {provider_id}",
        instance=instance,
    )


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


def provider_usage(
    instance: ServerInstance,
    connections: Sequence[str] | None = None,
) -> CommandResult:
    """Return live Provider subscription usage from `provider.usage` RPC."""

    params: dict[str, Any] = {}
    if connections is not None:
        params["connections"] = list(connections)
    payload = _rpc_call(instance, "provider.usage", params)
    if not payload.ok:
        return payload.to_command_result()

    providers = payload.data.get("providers")
    if not isinstance(providers, list):
        return CommandResult(
            ok=False,
            message="RPC result missing providers list",
            instance=instance,
        )
    return CommandResult(
        ok=True,
        message=_format_usage_report(payload.data.get("generated_at"), providers),
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
        refresh = model_refresh(instance, provider_id)
        return CommandResult(
            ok=refresh.ok,
            message=f"{message}\n{refresh.message}",
            instance=instance,
            failure=refresh.failure,
        )

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


def provider_set_enabled(
    instance: ServerInstance,
    provider_id: str,
    enabled: bool,
    connection_id: str | None = None,
) -> CommandResult:
    """Enable or disable one provider connection via `connection.set_enabled` RPC.

    Without an explicit connection id, the provider's single connection is
    resolved automatically; a multi-connection provider requires --connection.
    """

    if connection_id is None:
        resolved, error = _resolve_single_connection_id(instance, provider_id)
        if error is not None:
            return error
        connection_id = resolved

    payload = _rpc_call(
        instance,
        "connection.set_enabled",
        {"provider_id": provider_id, "connection_id": connection_id, "enabled": enabled},
    )
    if not payload.ok:
        return payload.to_command_result()

    resolved_connection_id = _string_or_default(payload.data.get("connection_id"), "?")
    state = "enabled" if payload.data.get("enabled") else "disabled"
    lines = [f"{state} {resolved_connection_id}"]
    if "reachable" in payload.data:
        reachable = payload.data.get("reachable")
        if reachable is True:
            lines.append("endpoint reachable; model catalog refreshed")
        elif reachable is False:
            lines.append(
                "endpoint not reachable — the connection stays enabled; "
                "start the local service (e.g. Ollama) and its models appear automatically"
            )
    if payload.data.get("enabled") and not payload.data.get("configured"):
        lines.append(
            "no credential configured; inspect authentication with: vbot provider "
            f"status {provider_id} --connection {resolved_connection_id} (keep the "
            "same target options)"
        )
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def _resolve_single_connection_id(
    instance: ServerInstance,
    provider_id: str,
    *,
    oauth_only: bool = False,
) -> tuple[str | None, CommandResult | None]:
    """Resolve a provider's single connection id, or explain which to pass."""

    payload = _rpc_call(instance, "connection.list", {})
    if not payload.ok:
        return None, payload.to_command_result()
    connections = payload.data.get("connections")
    if not isinstance(connections, list):
        return None, CommandResult(
            ok=False,
            message="RPC result missing connections list",
            instance=instance,
        )
    provider_connections = _filter_connections(connections, provider_id, None)
    if not provider_connections:
        return None, CommandResult(
            ok=False,
            message=_format_status_not_found(provider_id, None, connections),
            instance=instance,
        )
    if oauth_only:
        provider_connections = [
            connection
            for connection in provider_connections
            if isinstance(connection, dict) and connection.get("type") == "oauth"
        ]
        if not provider_connections:
            return None, CommandResult(
                ok=False,
                message=(
                    f"Provider '{provider_id}' has no OAuth Connection. "
                    f"Inspect authentication with: vbot provider status {provider_id}; "
                    "API-key Connections use vbot provider key set <provider-id> --stdin. "
                    "Keep the same target options."
                ),
                instance=instance,
            )
    if len(provider_connections) > 1:
        candidate_ids = _connection_ids(provider_connections)
        return None, CommandResult(
            ok=False,
            message=(
                f"provider '{provider_id}' has multiple connections; pass --connection "
                f"with one of: {', '.join(candidate_ids)}"
            ),
            instance=instance,
        )
    connection = provider_connections[0]
    connection_id = connection.get("id") if isinstance(connection, dict) else None
    if not isinstance(connection_id, str):
        return None, CommandResult(
            ok=False,
            message="RPC result missing connection id",
            instance=instance,
        )
    return connection_id, None


def provider_connect(
    instance: ServerInstance,
    provider_id: str,
    connection_id: str | None = None,
    account: str | None = None,
) -> CommandResult:
    """Start the OAuth device flow via `provider.connect` RPC."""

    if connection_id is None:
        connection_id, error = _resolve_single_connection_id(instance, provider_id, oauth_only=True)
        if error is not None:
            return error

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
    follow_up_command = (
        f"vbot provider connection status {provider_id} --connection {connection_id}"
    )
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
                f"progress with: {follow_up_command} (keep the same target options)",
            ]
        ),
        instance=instance,
        attention=("Login started; browser authorization is still required",),
    )


def provider_disconnect(
    instance: ServerInstance,
    provider_id: str,
    connection_id: str | None = None,
    account: str | None = None,
) -> CommandResult:
    """Remove a stored OAuth token via `provider.disconnect` RPC."""

    if connection_id is None:
        connection_id, error = _resolve_single_connection_id(instance, provider_id, oauth_only=True)
        if error is not None:
            return error

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
    connection_id: str | None = None,
    account: str | None = None,
) -> CommandResult:
    """Show OAuth connection state via `provider.connection_status` RPC."""

    if connection_id is None:
        connection_id, error = _resolve_single_connection_id(instance, provider_id, oauth_only=True)
        if error is not None:
            return error

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
        attention=()
        if payload.data.get("connected")
        else ("Account is not connected; inspect the device-flow state",),
    )


def provider_usage_history(
    instance: ServerInstance,
    since: str | None = None,
    until: str | None = None,
) -> CommandResult:
    """Return recorded usage-limit observations from `provider.usage_history`."""

    params: dict[str, Any] = {}
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    payload = _rpc_call(instance, "provider.usage_history", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_usage_history(payload.data),
        instance=instance,
    )


def provider_usage_history_clear(instance: ServerInstance, confirm: bool) -> CommandResult:
    """Delete all recorded usage observations after explicit confirmation."""

    if not confirm:
        return CommandResult(
            ok=False,
            message=(
                "refusing to delete the provider usage history without confirmation; "
                "re-run with --yes"
            ),
            instance=instance,
        )
    payload = _rpc_call(instance, "provider.usage_history.clear", {})
    if not payload.ok:
        return payload.to_command_result()
    deleted_samples = _value_text(payload.data.get("deleted_samples"))
    deleted_files = _value_text(payload.data.get("deleted_files"))
    return CommandResult(
        ok=True,
        message=(
            f"deleted provider usage history: {deleted_samples} samples "
            f"across {deleted_files} files"
        ),
        instance=instance,
    )


def _format_usage_history(data: Mapping[str, Any]) -> str:
    samples = data.get("samples")
    if not isinstance(samples, list):
        samples = []
    lines = [
        "provider usage history:",
        f"generated_at: {_string_or_default(data.get('generated_at'), '?')}",
    ]
    if not samples:
        lines.append("no recorded usage samples in the selected window")
        return "\n".join(lines)
    for sample in samples:
        if not isinstance(sample, dict):
            lines.append("- invalid sample entry")
            continue
        sampled_at = _string_or_default(sample.get("sampled_at"), "?")
        providers = sample.get("providers")
        if not isinstance(providers, list) or not providers:
            lines.append(f"- {sampled_at}  (no usable connections)")
            continue
        lines.append(f"- {sampled_at}")
        for provider in providers:
            if not isinstance(provider, dict):
                lines.append("  - invalid provider entry")
                continue
            connection = _string_or_default(provider.get("connection"), "?")
            error = provider.get("error")
            if isinstance(error, str) and error:
                lines.append(f"  {connection}: error: {error}")
                continue
            windows = provider.get("windows")
            if not isinstance(windows, list) or not windows:
                lines.append(f"  - {connection}: windows: none")
                continue
            for window in windows:
                lines.append(f"  {connection}: {_format_usage_window(window).strip()}")
    return "\n".join(lines)


def _format_connection_rows(connections: Sequence[object], *, details: bool = True) -> str:
    if not connections:
        return "no connections configured"

    providers = {
        str(connection.get("provider_id", "?"))
        for connection in connections
        if isinstance(connection, dict)
    }
    lines = [f"Providers: {len(providers)} | Connections: {len(connections)}"]
    lines.append("Configured means enabled with credentials; upstream access is not tested here.")
    grouped: dict[str, list[object]] = {}
    for connection in connections:
        provider_id = (
            str(connection.get("provider_id", "?")) if isinstance(connection, dict) else "?"
        )
        grouped.setdefault(provider_id, []).append(connection)
    for provider_id, rows in grouped.items():
        lines.extend(["", provider_id])
        lines.extend(_format_connection_row(row, details=details) for row in rows)
    lines.extend(
        [
            "",
            "Details: vbot provider status <provider-id>",
            "All details: vbot provider list --details",
            "Subscription login: vbot provider connect <provider-id>",
            "API key: vbot provider key set <provider-id> --stdin",
            "Keep the same --host/--port options on follow-up commands.",
        ]
    )
    return "\n".join(lines)


def _format_usage_report(generated_at: object, providers: Sequence[object]) -> str:
    lines = [
        "provider usage:",
        f"generated_at: {_string_or_default(generated_at, '?')}",
    ]
    if not providers:
        lines.append("no supported usable connections")
        return "\n".join(lines)

    for provider in providers:
        if not isinstance(provider, dict):
            lines.append("- invalid provider usage entry")
            continue
        connection = _string_or_default(provider.get("connection"), "?")
        display_name = _string_or_default(provider.get("display_name"), connection)
        plan = _string_or_default(provider.get("plan"), "-")
        lines.append(f"- {display_name} ({connection})  plan: {plan}")
        error = provider.get("error")
        if isinstance(error, str) and error:
            lines.append(f"  error: {error}")
        windows = provider.get("windows")
        if not isinstance(windows, list) or not windows:
            if not error:
                lines.append("  windows: none")
            continue
        for window in windows:
            lines.append(_format_usage_window(window))
    return "\n".join(lines)


def _format_usage_window(window: object) -> str:
    if not isinstance(window, dict):
        return "  - invalid usage window"
    label = _string_or_default(window.get("label"), "?")
    used = window.get("used_percent")
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        used_text = f"{float(used):g}%"
        remaining_text = f"{max(0.0, 100.0 - float(used)):g}%"
    else:
        used_text = "?"
        remaining_text = "?"
    reset_at = _string_or_default(window.get("reset_at"), "-")
    return f"  - {label}: used={used_text} remaining={remaining_text} reset_at={reset_at}"


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


def _format_connection_row(connection: object, *, details: bool = True) -> str:
    if not isinstance(connection, dict):
        return "- invalid connection entry"

    connection_id = _string_or_default(connection.get("id"), "?")
    provider_id = _string_or_default(connection.get("provider_id"), "?")
    connection_type = _string_or_default(connection.get("type"), "?")
    label = _string_or_default(connection.get("label"), "?")
    enabled = "yes" if connection.get("enabled") else "no"
    usable = "yes" if connection.get("usable") else "no"
    state: Status
    if connection.get("enabled") is False:
        state, description = "info", "Disabled"
    elif connection.get("usable") is True:
        state, description = "success", "Configured"
    else:
        state, description = (
            "info",
            "Login required" if connection_type == "oauth" else "API key required",
        )
        if connection_type == "none":
            description = "Not enabled"
    if connection.get("reachable") is False:
        state, description = "warning", f"{description} · local service unreachable"
    elif "reachable" in connection and connection["reachable"] is None:
        description += " · reachability not checked"
    heading = status_line(state, f"{label} ({connection_id}) — {description}")
    if not details:
        accounts = connection.get("accounts")
        account_ids = (
            [str(account.get("id", "?")) for account in accounts if isinstance(account, dict)]
            if isinstance(accounts, list)
            else []
        )
        return heading + (f" | accounts: {', '.join(account_ids)}" if account_ids else "")
    header = (
        f"  id: {connection_id}"
        f"  type: {connection_type}"
        f"\n  provider_id: {provider_id}"
        f"  enabled: {enabled}"
        f"  usable: {usable}"
    )
    if "reachable" in connection:
        reachable = connection.get("reachable")
        reachable_text = "unknown" if reachable is None else ("yes" if reachable else "no")
        header = f"{header}  reachable: {reachable_text}"
    return "\n".join([heading, header, _format_account_rows(connection.get("accounts"))])


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
