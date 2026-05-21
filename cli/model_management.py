"""Model management RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0


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
