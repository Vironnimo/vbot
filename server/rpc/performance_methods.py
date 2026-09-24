"""Performance measurement RPC handlers.

``performance.snapshot`` returns the process-wide histograms, gauges, recent
Event Loop stalls and the active recording. ``performance.recording_start`` /
``performance.recording_stop`` control the single trace recording, and
``performance.recording_list`` reads the retained recordings. File work runs
inside the Runtime-owned performance service, off the Event Loop.
"""

from __future__ import annotations

from typing import Any, cast

from core.performance import (
    DEFAULT_RECORDING_SECONDS,
    MAX_RECORDING_SECONDS,
    RETAINED_RECORDINGS,
    PerformanceService,
)
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_positive_integer,
    _optional_string,
    _reject_unsupported,
)

JsonObject = dict[str, Any]
_MAX_LIST_LIMIT = 100


def _performance(state: Any) -> PerformanceService:
    return cast(PerformanceService, state.runtime.performance)


async def _snapshot(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "performance.snapshot")
    return await _performance(state).snapshot()


def _recording_start(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"label", "max_seconds"}, "performance.recording_start")
    label = _optional_string(params, "label")
    max_seconds = _optional_positive_integer(params, "max_seconds", max_value=MAX_RECORDING_SECONDS)
    try:
        return _performance(state).start_recording(
            label=label,
            max_seconds=DEFAULT_RECORDING_SECONDS if max_seconds is None else max_seconds,
        )
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{exc}") from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _recording_stop(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "performance.recording_stop")
    try:
        return await _performance(state).stop_recording()
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _recording_list(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"limit"}, "performance.recording_list")
    limit = _optional_positive_integer(params, "limit", max_value=_MAX_LIST_LIMIT)
    recordings = await _performance(state).list_recordings(
        limit=RETAINED_RECORDINGS if limit is None else limit
    )
    return {"recordings": recordings}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the performance measurement handlers."""
    return {
        "performance.snapshot": _snapshot,
        "performance.recording_start": _recording_start,
        "performance.recording_stop": _recording_stop,
        "performance.recording_list": _recording_list,
    }
