"""Performance measurement RPC handlers.

``performance.snapshot`` returns the process-wide histograms, gauges, recent
Event Loop stalls and the active recording. ``performance.recording_start`` /
``performance.recording_stop`` control the single trace recording, and
``performance.recording_list`` reads the retained recordings.
``performance.heap`` counts the objects the garbage collector tracks, and
``performance.history`` reads the stored window summaries. File and census
work runs inside the Runtime-owned performance service, off the Event Loop.
``performance.client_report`` merges a WebUI client's batched measurements
into the process sink under ``webui.*`` names.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Collection
from datetime import datetime
from typing import Any, cast

from core.performance import (
    DEFAULT_HEAP_TOP,
    DEFAULT_HISTORY_WINDOWS,
    DEFAULT_RECORDING_SECONDS,
    MAX_HEAP_TOP,
    MAX_HISTORY_NAMES,
    MAX_HISTORY_WINDOWS,
    MAX_RECORDING_SECONDS,
    RETAINED_RECORDINGS,
    ExternalHistogram,
    PerformanceService,
    count,
    merge_histogram,
)
from server.events import ALLOWED_RESOURCE_KINDS
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _optional_positive_integer,
    _optional_string,
    _reject_unsupported,
    _required_string_list,
)

JsonObject = dict[str, Any]
_MAX_LIST_LIMIT = 100

CLIENT_TRACK = "webui"
CLIENT_REPORTS_METRIC = "webui.reports"
CLIENT_REPORTS_DROPPED_METRIC = "webui.reports_dropped"
# Client names come from fixed sets so a client cannot grow the metric set.
CLIENT_HISTOGRAMS = frozenset({"webui.rpc", "webui.long_task", "webui.long_animation_frame"})
CLIENT_COUNTERS = frozenset({"webui.rpc_errors"})
CLIENT_INVALIDATIONS_PREFIX = "webui.invalidations."
CLIENT_INVALIDATION_RPCS_PREFIX = "webui.invalidation_rpcs."
CLIENT_PAGE_INVALIDATIONS_PREFIX = "webui.extension_page.invalidations."
CLIENT_PAGE_INVALIDATION_REASONS = frozenset(
    {"reload", "descriptor_changed", "disposed", "run_stream_recovered", "change"}
)
MAX_CLIENT_REPORT_NAMES = 200
MAX_CLIENT_REPORT_AMOUNT = 1_000_000
CLIENT_REPORTS_PER_MINUTE = 60
_MAX_SKIPPED_NAMES = 20


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


async def _heap(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"top"}, "performance.heap")
    top = _optional_positive_integer(params, "top", max_value=MAX_HEAP_TOP)
    return await _performance(state).heap_census(top=DEFAULT_HEAP_TOP if top is None else top)


async def _history(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"since", "until", "limit", "names"}, "performance.history")
    since = _optional_timestamp(params, "since")
    until = _optional_timestamp(params, "until")
    limit = _optional_positive_integer(params, "limit", max_value=MAX_HISTORY_WINDOWS)
    names = _required_string_list(params, "names") if "names" in params else None
    if names is not None and len(names) > MAX_HISTORY_NAMES:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, f"params.names must list at most {MAX_HISTORY_NAMES} names"
        )
    return await _performance(state).history(
        since=since,
        until=until,
        limit=DEFAULT_HISTORY_WINDOWS if limit is None else limit,
        names=names,
    )


def _optional_timestamp(params: JsonObject, key: str) -> datetime | None:
    value = _optional_string(params, key)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be an ISO 8601 timestamp with a time zone offset",
        )
    return parsed


class _ReportLimiter:
    """At most ``limit`` accepted reports per sliding minute, across all clients."""

    def __init__(self, limit: int, clock: Any = time.monotonic) -> None:
        self._limit = limit
        self._clock = clock
        self._accepted: deque[float] = deque()
        self._lock = threading.Lock()

    def admit(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._accepted and now - self._accepted[0] >= 60.0:
                self._accepted.popleft()
            if len(self._accepted) >= self._limit:
                return False
            self._accepted.append(now)
        return True


_CLIENT_REPORTS = _ReportLimiter(CLIENT_REPORTS_PER_MINUTE)


def _client_report(state: Any, params: JsonObject) -> JsonObject:
    del state
    _reject_unsupported(params, {"histograms", "counters"}, "performance.client_report")
    histograms = _client_section(params, "histograms")
    counters = _client_section(params, "counters")
    if len(histograms) + len(counters) > MAX_CLIENT_REPORT_NAMES:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"a client report carries at most {MAX_CLIENT_REPORT_NAMES} names",
        )
    parsed_histograms = {name: _client_histogram(name, value) for name, value in histograms.items()}
    parsed_counters = {name: _client_amount(name, value) for name, value in counters.items()}
    if not _CLIENT_REPORTS.admit():
        count(CLIENT_REPORTS_DROPPED_METRIC)
        return {"accepted": 0, "skipped": [], "rate_limited": True}

    accepted = 0
    skipped: list[str] = []
    for name, histogram in parsed_histograms.items():
        if name in CLIENT_HISTOGRAMS:
            merge_histogram(name, histogram)
            accepted += 1
        else:
            skipped.append(name)
    for name, amount in parsed_counters.items():
        if _known_client_counter(name):
            count(name, amount)
            accepted += 1
        else:
            skipped.append(name)
    rpc = parsed_histograms.get("webui.rpc")
    long_tasks = parsed_histograms.get("webui.long_task")
    count(
        CLIENT_REPORTS_METRIC,
        track=CLIENT_TRACK,
        args={
            "rpcs": rpc.count if rpc else 0,
            "long_tasks": long_tasks.count if long_tasks else 0,
            "long_task_max_ms": long_tasks.max_ms if long_tasks else 0,
            "invalidations": sum(
                amount
                for name, amount in parsed_counters.items()
                if name.startswith(CLIENT_INVALIDATIONS_PREFIX)
            ),
        },
    )
    # Unknown names come from a newer or older WebUI; the rest still counts.
    return {"accepted": accepted, "skipped": sorted(skipped)[:_MAX_SKIPPED_NAMES]}


def _client_section(params: JsonObject, key: str) -> JsonObject:
    value = params.get(key, {})
    if not isinstance(value, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be an object")
    return value


def _client_amount(name: str, value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_CLIENT_REPORT_AMOUNT
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"{name} must be an integer from 1 to {MAX_CLIENT_REPORT_AMOUNT}",
        )
    return value


def _client_histogram(name: str, value: Any) -> ExternalHistogram:
    fields = {"count", "sum_ms", "min_ms", "max_ms", "buckets"}
    if not isinstance(value, dict) or set(value) != fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST, f"{name} must be an object with {', '.join(sorted(fields))}"
        )
    durations = [value[key] for key in ("sum_ms", "min_ms", "max_ms")]
    if not all(_finite_number(duration) for duration in durations):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{name} durations must be finite numbers")
    raw_buckets = value["buckets"]
    if not isinstance(raw_buckets, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{name}.buckets must be an object")
    try:
        buckets = {
            int(index): _client_amount(name, amount) for index, amount in raw_buckets.items()
        }
        return ExternalHistogram(
            count=_client_amount(name, value["count"]),
            total_ms=float(value["sum_ms"]),
            min_ms=float(value["min_ms"]),
            max_ms=float(value["max_ms"]),
            buckets=buckets,
        )
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{name}: {exc}") from exc


def _finite_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _known_client_counter(name: str) -> bool:
    if name in CLIENT_COUNTERS:
        return True
    if name.startswith(CLIENT_INVALIDATIONS_PREFIX):
        return name.removeprefix(CLIENT_INVALIDATIONS_PREFIX) in ALLOWED_RESOURCE_KINDS
    if name.startswith(CLIENT_INVALIDATION_RPCS_PREFIX):
        kind, _, method = name.removeprefix(CLIENT_INVALIDATION_RPCS_PREFIX).partition(".")
        return kind in ALLOWED_RESOURCE_KINDS and method in _rpc_methods()
    if name.startswith(CLIENT_PAGE_INVALIDATIONS_PREFIX):
        reason = name.removeprefix(CLIENT_PAGE_INVALIDATIONS_PREFIX)
        return reason in CLIENT_PAGE_INVALIDATION_REASONS
    return False


def _rpc_methods() -> Collection[str]:
    # The method table imports this module, so it is read at call time.
    from server.rpc.methods import METHODS

    return METHODS.keys()


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the performance measurement handlers."""
    return {
        "performance.snapshot": _snapshot,
        "performance.recording_start": _recording_start,
        "performance.recording_stop": _recording_stop,
        "performance.recording_list": _recording_list,
        "performance.heap": _heap,
        "performance.history": _history,
        "performance.client_report": _client_report,
    }
