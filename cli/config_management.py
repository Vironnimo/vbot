"""Config management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def config_raw(instance: ServerInstance) -> CommandResult:
    """Print raw settings.json contents via settings.get_raw RPC."""

    payload = _rpc_call(instance, "settings.get_raw", {})
    if not payload.ok:
        return payload.to_command_result()

    settings = payload.data.get("settings", {})
    if not isinstance(settings, dict):
        settings = {}

    return CommandResult(ok=True, message=json.dumps(settings, indent=2), instance=instance)


def config_effective(instance: ServerInstance) -> CommandResult:
    """Print the normalized public Settings document."""

    payload = _rpc_call(instance, "settings.values", {})
    if not payload.ok:
        return payload.to_command_result()
    settings = payload.data.get("settings", {})
    return CommandResult(
        ok=True,
        message=json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True),
        instance=instance,
    )


def config_list(instance: ServerInstance, prefix: str | None = None) -> CommandResult:
    """List public Settings paths and their catalog metadata."""

    params = {"prefix": prefix} if prefix else {}
    payload = _rpc_call(instance, "settings.catalog", params)
    if not payload.ok:
        return payload.to_command_result()
    settings = payload.data.get("settings")
    if not isinstance(settings, list):
        return CommandResult(
            ok=False,
            message="RPC result missing settings catalog",
            instance=instance,
        )
    lines = [_format_catalog_entry(entry) for entry in settings if isinstance(entry, dict)]
    return CommandResult(
        ok=True,
        message="\n".join(lines) if lines else "no settings paths",
        instance=instance,
    )


def config_describe(instance: ServerInstance, path: str) -> CommandResult:
    """Show complete metadata for one public Settings path."""

    payload = _rpc_call(
        instance,
        "settings.get_path",
        {"path": path, "allow_missing": True},
    )
    if not payload.ok:
        return payload.to_command_result()
    setting = payload.data.get("setting")
    if not isinstance(setting, dict):
        return CommandResult(
            ok=False,
            message=f"RPC result missing settings metadata: {path}",
            instance=instance,
        )
    return CommandResult(
        ok=True,
        message=_format_setting_details(setting),
        instance=instance,
    )


def config_get(
    instance: ServerInstance,
    path: str,
    *,
    details: bool = False,
) -> CommandResult:
    """Get one public Settings path, optionally with catalog metadata."""

    payload = _rpc_call(instance, "settings.get_path", {"path": path})
    if not payload.ok:
        return payload.to_command_result()
    setting = payload.data.get("setting")
    if not isinstance(setting, dict) or "value" not in setting:
        return CommandResult(
            ok=False,
            message=f"RPC result missing settings value: {path}",
            instance=instance,
        )
    message = _format_setting_details(setting) if details else _json_value(setting["value"])
    return CommandResult(ok=True, message=message, instance=instance)


def config_set(instance: ServerInstance, path: str, value: Any) -> CommandResult:
    """Set one public Settings path through the atomic patch contract."""

    return config_patch(instance, [{"op": "set", "path": path, "value": value}])


def config_unset(instance: ServerInstance, path: str) -> CommandResult:
    """Remove one configured override through the atomic patch contract."""

    return config_patch(instance, [{"op": "unset", "path": path}])


def config_patch(instance: ServerInstance, operations: list[dict[str, Any]]) -> CommandResult:
    """Apply one or more public Settings changes atomically."""

    payload = _rpc_call(instance, "settings.patch", {"operations": operations})
    if not payload.ok:
        return payload.to_command_result()
    changes = payload.data.get("changes")
    changed = payload.data.get("changed")
    if not isinstance(changes, list) or not isinstance(changed, list):
        return CommandResult(
            ok=False,
            message="RPC result missing settings patch result",
            instance=instance,
        )

    changed_paths = {path for path in changed if isinstance(path, str)}
    lines = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "<unknown>"))
        status = "updated" if path in changed_paths else "unchanged"
        value = _json_value(item.get("value"))
        lines.append(f"{status}: {path} = {value}")
        if "pending_value" in item:
            lines.append(f"  pending: {_json_value(item['pending_value'])}")
        lines.append(f"  application: {item.get('application', 'unknown')}")
    restart_required = payload.data.get("restart_required") is True
    lines.append(f"restart_required: {'yes' if restart_required else 'no'}")
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def coerce_config_value(raw: str) -> Any:
    """Coerce a CLI string to a JSON-native type, falling back to plain string."""

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _format_catalog_entry(entry: dict[str, Any]) -> str:
    path = str(entry.get("path", "<unknown>"))
    metadata = [str(entry.get("type", "unknown")), str(entry.get("application", "unknown"))]
    if "source" in entry:
        metadata.append(f"source={entry['source']}")
    value = f" = {_json_value(entry['value'])}" if "value" in entry else ""
    return f"{path}{value} ({', '.join(metadata)})"


def _format_setting_details(setting: dict[str, Any]) -> str:
    ordered_fields = (
        "path",
        "value",
        "configured",
        "configured_value",
        "pending_value",
        "source",
        "default",
        "type",
        "allowed_values",
        "nullable",
        "unsettable",
        "application",
        "restart_required",
        "description",
    )
    lines = []
    for field in ordered_fields:
        if field not in setting:
            continue
        lines.append(f"{field}: {_plain_or_json(setting[field])}")
    return "\n".join(lines)


def _plain_or_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _json_value(value)


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
