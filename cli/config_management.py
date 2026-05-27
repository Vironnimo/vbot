"""Config management RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping
from difflib import get_close_matches
from typing import Any

import httpx

from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0


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
