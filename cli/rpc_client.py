"""Shared RPC transport client for CLI management commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from cli.server_management import CommandResult, ServerInstance

RPC_PATH = "/api/rpc"
RPC_TIMEOUT_SECONDS = 10.0

# Methods that legitimately run far longer than the default cap. `model.refresh_db`
# fans out sequentially to provider /models endpoints, each server-bounded to 60s
# with retries, so the total can exceed any fixed ceiling. These calls leave the
# read phase unbounded — the server already bounds the work — while a short connect
# timeout still fails fast on an unreachable server. This is the same read=None
# shape the provider chat client uses for open-ended generation.
_LONG_RUNNING_METHODS: frozenset[str] = frozenset({"model.refresh_db"})
RPC_LONG_RUNNING_TIMEOUT = httpx.Timeout(RPC_TIMEOUT_SECONDS, read=None)


class RpcPayload:
    """Normalized server RPC success or failure payload."""

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


def rpc_call(instance: ServerInstance, method: str, params: dict[str, Any]) -> RpcPayload:
    """Call one server RPC method and return normalized success/error payload."""

    request_body = {"method": method, "params": params}
    timeout: httpx.Timeout | float = (
        RPC_LONG_RUNNING_TIMEOUT if method in _LONG_RUNNING_METHODS else RPC_TIMEOUT_SECONDS
    )
    try:
        response = httpx.post(
            f"{instance.url}{RPC_PATH}",
            json=request_body,
            timeout=timeout,
            # The CLI only ever talks to the local server over loopback, and RPC bodies
            # carry secrets (e.g. provider.set_key). Ignore ambient HTTP_PROXY/.netrc so a
            # plaintext credential can never be diverted through an environment proxy — the
            # same hardening the health/webui probes already apply.
            trust_env=False,
        )
    except httpx.RequestError as exc:
        return RpcPayload(
            ok=False,
            instance=instance,
            message=f"RPC request failed: {exc.__class__.__name__}",
        )

    try:
        payload = response.json()
    except ValueError:
        return RpcPayload(
            ok=False,
            instance=instance,
            message=f"RPC response was not JSON (HTTP {response.status_code})",
        )

    if not isinstance(payload, dict):
        return RpcPayload(ok=False, instance=instance, message="RPC response must be an object")

    if response.status_code != httpx.codes.OK:
        return RpcPayload(
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
            return RpcPayload(ok=False, instance=instance, message="RPC result must be an object")
        return RpcPayload(ok=True, instance=instance, data=result)
    if ok_flag is False:
        return RpcPayload(
            ok=False,
            instance=instance,
            message=_rpc_error_message(payload.get("error"), fallback="RPC request failed"),
        )

    return RpcPayload(ok=False, instance=instance, message="RPC response missing boolean ok flag")


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
