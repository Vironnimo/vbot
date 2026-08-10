"""RPC envelope parsing and method invocation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from inspect import isawaitable
from typing import Any

from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_METHOD_NOT_FOUND,
    RpcError,
)

JsonObject = dict[str, Any]
RpcMethodHandler = Callable[[Any, JsonObject], JsonObject | Awaitable[JsonObject]]

_LOGGER = logging.getLogger("vbot.server.rpc.dispatcher")


async def dispatch_rpc(
    state: Any,
    request: Any,
    handlers: Mapping[str, RpcMethodHandler],
) -> JsonObject:
    """Dispatch one JSON-RPC-like vBot server request."""

    method_name = _request_method_for_log(request)
    try:
        method, params = parse_rpc_request(request)
        result = await dispatch_method(state, method, params, handlers)
    except RpcError as exc:
        _LOGGER.warning(
            "RPC request rejected (method=%s code=%s)",
            method_name,
            exc.code,
        )
        return {"ok": False, "error": exc.to_dict()}
    except Exception:
        _LOGGER.exception("Unexpected RPC request failure (method=%s)", method_name)
        raise
    return {"ok": True, "result": result}


def _request_method_for_log(request: Any) -> str:
    if isinstance(request, dict):
        method = request.get("method")
        if isinstance(method, str) and method:
            return method
    return "<invalid>"


async def dispatch_method(
    state: Any,
    method: str,
    params: JsonObject,
    handlers: Mapping[str, RpcMethodHandler],
) -> JsonObject:
    """Invoke one registered RPC method."""

    handler = handlers.get(method)
    if handler is None:
        raise RpcError(RPC_ERROR_METHOD_NOT_FOUND, f"unknown RPC method: {method}")

    result = handler(state, params)
    if isawaitable(result):
        result = await result
    return result


def parse_rpc_request(request: Any) -> tuple[str, JsonObject]:
    """Parse and validate the RPC request envelope."""

    if not isinstance(request, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC request must be a JSON object")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC method must be a non-empty string")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC params must be an object")
    return method, params
