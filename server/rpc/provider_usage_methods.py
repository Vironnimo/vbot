"""Provider usage RPC handler.

``provider.usage`` returns each logged-in provider connection's own live usage
windows (5h / weekly percentage, reset time, plan), fetched on demand from the
providers domain (see ``.vorch/domain-maps/providers.md`` → Provider Usage
Probe). It is **live provider state**, deliberately separate from
``statistics.report`` (a read-only aggregation over persisted Sessions).
"""

from __future__ import annotations

from typing import Any, cast

from core.providers.usage import ProviderUsageService
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]

_SUPPORTED_FIELDS = {"connections"}


async def _provider_usage(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - _SUPPORTED_FIELDS)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider.usage fields: {', '.join(unsupported_fields)}",
        )

    connections = _optional_connections(params)
    report = await _usage_service(state).report(connections=connections)
    return report.to_dict()


def _optional_connections(params: JsonObject) -> list[str] | None:
    value = params.get("connections")
    if value is None:
        return None
    if not isinstance(value, list) or not all(
        isinstance(connection, str) and connection for connection in value
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.connections must be a list of connection id strings",
        )
    return value


def _usage_service(state: Any) -> ProviderUsageService:
    service = getattr(state, "usage_service", None)
    if service is not None:
        return cast(ProviderUsageService, service)
    service = ProviderUsageService(state.runtime)
    state.usage_service = service
    return service


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the provider usage RPC handlers."""

    return {"provider.usage": _provider_usage}
