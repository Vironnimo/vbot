"""Statistics RPC handler.

``statistics.report`` returns a full read-only :class:`StatisticsReport` computed
on demand from persisted Sessions (see ``.vorch/domain-maps/statistics.md``). It accepts
an optional ``{since, until}`` ISO-8601 UTC window and contains no opaque provider
metadata by construction (no raw tool arguments, no reasoning data).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from core.statistics import StatisticsService
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError

JsonObject = dict[str, Any]

_SUPPORTED_FIELDS = {"since", "until"}


def _statistics_report(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - _SUPPORTED_FIELDS)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported statistics.report fields: {', '.join(unsupported_fields)}",
        )

    since = _optional_utc_timestamp(params, "since")
    until = _optional_utc_timestamp(params, "until")
    if since is not None and until is not None and since > until:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.since must not be after params.until")

    report = _statistics_service(state).report(since=since, until=until)
    return report.to_dict()


def _optional_utc_timestamp(params: JsonObject, key: str) -> datetime | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be an ISO 8601 timestamp string",
        )
    parsed = _parse_iso_utc(value)
    if parsed is None:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be an ISO 8601 timestamp string",
        )
    return parsed


def _parse_iso_utc(value: str) -> datetime | None:
    try:
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _statistics_service(state: Any) -> StatisticsService:
    service = getattr(state, "statistics_service", None)
    if service is not None:
        return cast(StatisticsService, service)
    service = StatisticsService(state.runtime.chat_sessions, state.runtime.agents)
    state.statistics_service = service
    return service


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the statistics RPC handlers."""

    return {"statistics.report": _statistics_report}
