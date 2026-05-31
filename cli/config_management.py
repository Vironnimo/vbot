"""Config management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from difflib import get_close_matches
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def config_show(instance: ServerInstance) -> CommandResult:
    """Print raw settings.json contents via settings.get_raw RPC."""

    payload = _rpc_call(instance, "settings.get_raw", {})
    if not payload.ok:
        return payload.to_command_result()

    settings = payload.data.get("settings", {})
    if not isinstance(settings, dict):
        settings = {}

    return CommandResult(ok=True, message=json.dumps(settings, indent=2), instance=instance)


def config_get(instance: ServerInstance, key: str) -> CommandResult:
    """Get a single raw settings key via settings.get_raw RPC."""

    payload = _rpc_call(instance, "settings.get_raw", {})
    if not payload.ok:
        return payload.to_command_result()

    settings = payload.data.get("settings", {})
    if not isinstance(settings, dict) or key not in settings:
        candidates = sorted(settings) if isinstance(settings, dict) else []
        return CommandResult(
            ok=False,
            message=_format_missing_key(key, candidates),
            instance=instance,
        )

    return CommandResult(ok=True, message=json.dumps(settings[key]), instance=instance)


def config_set(instance: ServerInstance, key: str, value: Any) -> CommandResult:
    """Set a single raw settings key via settings.set_key RPC."""

    payload = _rpc_call(instance, "settings.set_key", {"key": key, "value": value})
    if not payload.ok:
        return payload.to_command_result()

    return CommandResult(ok=True, message=f"{key} = {json.dumps(value)}", instance=instance)


def coerce_config_value(raw: str) -> Any:
    """Coerce a CLI string to a JSON-native type, falling back to plain string."""

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _format_missing_key(key: str, candidates: list[str]) -> str:
    lines = [f"key '{key}' not found"]
    if candidates:
        lines.append(f"available keys: {', '.join(candidates)}")
        suggestions = get_close_matches(key, candidates, n=1)
        if suggestions:
            lines.append(f"did you mean: {suggestions[0]}")
    return "\n".join(lines)
