"""Provider usage RPC handlers.

``provider.usage`` returns each logged-in provider connection's own live usage
windows, plan, account, unit counts, and credits. ``provider.usage_history``
reads the Provider-owned automatic observations, while its ``.clear`` sibling
explicitly deletes them. All three remain separate from ``statistics.report``
(a read-only aggregation over persisted Sessions).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from core.providers.usage import ProviderUsageService
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]

_SUPPORTED_FIELDS = {"connections"}
_HISTORY_SUPPORTED_FIELDS = {"since", "until"}


async def _provider_usage(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _SUPPORTED_FIELDS, "provider.usage")

    connections = _optional_connections(params)
    report = await _usage_service(state).report(connections=connections)
    return report.to_dict()


def _provider_usage_history(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, _HISTORY_SUPPORTED_FIELDS, "provider.usage_history")
    since = _optional_utc_timestamp(params, "since")
    until = _optional_utc_timestamp(params, "until")
    if since is not None and until is not None and since > until:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.since must not be after params.until")
    return _usage_service(state).history_report(since=since, until=until).to_dict()


def _provider_usage_history_clear(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "provider.usage_history.clear")
    return _usage_service(state).clear_history().to_dict()


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


def _optional_utc_timestamp(params: JsonObject, key: str) -> datetime | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be an ISO 8601 timestamp string",
        )
    try:
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be an ISO 8601 timestamp string",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _usage_service(state: Any) -> ProviderUsageService:
    service = getattr(state, "usage_service", None)
    if service is not None:
        return cast(ProviderUsageService, service)
    return cast(ProviderUsageService, state.runtime.provider_usage)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the provider usage RPC handlers."""

    return {
        "provider.usage": _provider_usage,
        "provider.usage_history": _provider_usage_history,
        "provider.usage_history.clear": _provider_usage_history_clear,
    }
