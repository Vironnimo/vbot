"""Model management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def model_list(instance: ServerInstance) -> CommandResult:
    """List available models via `model.list` RPC."""

    payload = _rpc_call(instance, "model.list", {})
    if not payload.ok:
        return payload.to_command_result()
    models = payload.data.get("models")
    if not isinstance(models, list):
        return CommandResult(ok=False, message="RPC result missing models list", instance=instance)
    return CommandResult(ok=True, message=_format_model_rows(models), instance=instance)


def model_refresh(instance: ServerInstance, provider_id: str | None = None) -> CommandResult:
    """Refresh model database via `model.refresh_db` RPC."""

    params: dict[str, Any] = {}
    if provider_id is not None:
        params["provider_id"] = provider_id
    payload = _rpc_call(instance, "model.refresh_db", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_refresh_result(payload.data, provider_id),
        instance=instance,
    )


def _format_model_rows(models: Sequence[object]) -> str:
    if not models:
        return "no models available"

    lines = ["models:"]
    for model in models:
        lines.append(_format_model_row(model))
    return "\n".join(lines)


def _format_model_row(model: object) -> str:
    if not isinstance(model, dict):
        return "- invalid model entry"

    model_id = _string_or_default(model.get("id"), "?")
    name = _string_or_default(model.get("name"), "?")
    context_window = _stringify_or_default(model.get("context_window"), "?")
    return f"- id: {model_id}  name: {name}  context_window: {context_window}"


def _format_refresh_result(data: Mapping[str, Any], provider_id: str | None) -> str:
    if provider_id is not None:
        resolved_provider_id = _string_or_default(data.get("provider_id"), provider_id)
        return f"refreshed {resolved_provider_id}"

    refreshed_count = data.get("refreshed_count", "?")
    model_count = data.get("model_count", "?")
    return f"refreshed {refreshed_count} providers ({model_count} models)"


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default


def _stringify_or_default(value: object, default: str) -> str:
    if value is None:
        return default
    return str(value)
