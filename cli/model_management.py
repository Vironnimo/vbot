"""Model management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cli.formatting import string_or_default as _string_or_default
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def model_list(
    instance: ServerInstance,
    filters: Mapping[str, Any] | None = None,
) -> CommandResult:
    """List available models via `model.list` RPC."""

    payload = _rpc_call(instance, "model.list", dict(filters or {}))
    if not payload.ok:
        return payload.to_command_result()
    models = payload.data.get("models")
    if not isinstance(models, list):
        return CommandResult(ok=False, message="RPC result missing models list", instance=instance)
    return CommandResult(ok=True, message=_format_model_rows(models), instance=instance)


def model_refresh(
    instance: ServerInstance,
    provider_id: str | None = None,
    *,
    target: str | None = None,
    expected_resources_dir: Path | None = None,
) -> CommandResult:
    """Refresh model database via `model.refresh_db` RPC."""

    params: dict[str, Any] = {}
    if provider_id is not None:
        params["provider_id"] = provider_id
    if target is not None:
        params["target"] = target
    if expected_resources_dir is not None:
        params["expected_resources_dir"] = str(expected_resources_dir.resolve())
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
    context_window = _stringify_or_default(
        model.get("effective_context_window", model.get("context_window")), "?"
    )
    fields = [f"- id: {model_id}", f"name: {name}", f"context_window: {context_window}"]
    if "reachable" in model:
        fields.append(f"reachable: {'yes' if model.get('reachable') else 'no'}")
    capabilities = _capability_names(model.get("capabilities"))
    if capabilities:
        fields.append(f"capabilities: {','.join(capabilities)}")
    tasks = _string_items(model.get("capabilities"), "task_types")
    if tasks:
        fields.append(f"tasks: {','.join(tasks)}")
    return "  ".join(fields)


def _capability_names(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    names = [name for name in ("vision", "tools", "json_mode") if value.get(name) is True]
    reasoning = value.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("supported") is True:
        names.append("reasoning")
    return names


def _string_items(value: object, key: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    items = value.get(key)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, str) and item]


def _format_refresh_result(data: Mapping[str, Any], provider_id: str | None) -> str:
    failures = _format_refresh_failures(data.get("errors"))
    if provider_id is not None:
        resolved_provider_id = _string_or_default(data.get("provider_id"), provider_id)
        return f"refreshed {resolved_provider_id}{failures}"

    refreshed_count = data.get("refreshed_count", "?")
    model_count = data.get("model_count", "?")
    return f"refreshed {refreshed_count} providers ({model_count} models){failures}"


def _format_refresh_failures(errors: object) -> str:
    """Render a "; N failed: …" suffix for connections discovery skipped.

    A broken provider no longer aborts the refresh; it is reported instead so
    an agent reading the CLI output knows which connections were left stale.
    """

    if not isinstance(errors, Sequence) or isinstance(errors, str | bytes) or not errors:
        return ""
    labels = []
    for entry in errors:
        if isinstance(entry, Mapping):
            label = entry.get("connection_id") or entry.get("provider_id") or "unknown"
        else:
            label = "unknown"
        labels.append(str(label))
    return f"; {len(labels)} failed: {', '.join(labels)}"


def _stringify_or_default(value: object, default: str) -> str:
    if value is None:
        return default
    return str(value)
