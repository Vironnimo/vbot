"""Server-side performance recording through vBot's ``performance.*`` RPCs.

The harness starts one recording right before a level's load phase and stops
it right after. The stop result carries a metric summary (timings, gauge
maxima, Event Loop stalls with sampled stacks) and the path of the full trace
file, which is copied into the level's result folder.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.perf_load_suite.rpc import RpcCaller, RpcCallError

RECORDING_ACTIVE_CODE = "performance_recording_active"
RECORDING_INACTIVE_CODE = "performance_recording_inactive"
METHOD_NOT_FOUND_CODE = "method_not_found"
TOP_WORKER_POOLS = 5
TOP_METRICS = 15
STALL_FRAMES = 12
_WORKER_POOL_PREFIX = "worker_pool."
_TOOL_PREFIX = "tool."
# Digest key -> server histogram name (see the Performance domain's metric catalog).
DIGEST_METRICS: dict[str, str] = {
    "event_loop_lag_ms": "event_loop.lag",
    "sqlite_write_ms": "sqlite.sessions.write",
    "sqlite_write_wait_ms": "sqlite.sessions.write_wait",
    "sqlite_read_ms": "sqlite.sessions.read",
    "chat_run_ms": "chat.run",
    "chat_request_build_ms": "chat.request_build",
    "provider_first_token_ms": "provider.first_token",
    "provider_response_ms": "provider.response",
    "chat_persist_ms": "chat.persist",
    "chat_tool_round_ms": "chat.tool_round",
    "chat_compaction_ms": "chat.compaction",
}
# Digest key -> server gauge name; a recording reports each gauge's maximum.
DIGEST_GAUGES: dict[str, str] = {
    "event_loop_utilization_max": "event_loop.utilization",
    "process_cpu_percent_max": "process.cpu_percent",
    "process_rss_mb_max": "process.rss_mb",
    "process_python_threads_max": "process.python_threads",
    "asyncio_tasks_max": "asyncio.tasks",
    "runs_active_max": "runs.active",
    "runs_queued_max": "runs.queued",
}


class RecordingError(RuntimeError):
    """The server-side recording could not be started, stopped or read."""


def check_instrumentation(rpc: RpcCaller) -> dict[str, Any]:
    """Fail fast when the target server has no performance instrumentation."""
    try:
        snapshot = rpc.call("performance.snapshot", {})
    except RpcCallError as exc:
        if exc.code == METHOD_NOT_FOUND_CODE:
            raise RecordingError(
                "this vBot server has no performance.* RPCs; update the checkout that "
                "runs the harness"
            ) from exc
        raise RecordingError(f"performance.snapshot failed: {exc}") from exc
    if snapshot.get("recording") is not None:
        raise RecordingError("a performance recording is already active on the new server")
    return dict(snapshot)


def start_recording(rpc: RpcCaller, *, label: str, max_seconds: float) -> dict[str, Any]:
    """Start the level's recording; the server must not already be recording."""
    try:
        status = rpc.call(
            "performance.recording_start", {"label": label, "max_seconds": max_seconds}
        )
    except RpcCallError as exc:
        if exc.code == RECORDING_ACTIVE_CODE:
            raise RecordingError("another performance recording is already active") from exc
        raise RecordingError(f"performance.recording_start failed: {exc}") from exc
    return dict(status)


def stop_recording(rpc: RpcCaller) -> dict[str, Any]:
    """Stop the level's recording and return the server's full stop result."""
    try:
        result = rpc.call("performance.recording_stop", {})
    except RpcCallError as exc:
        if exc.code == RECORDING_INACTIVE_CODE:
            raise RecordingError(
                "the recording ended before the load phase finished; raise --recording-max-seconds"
            ) from exc
        raise RecordingError(f"performance.recording_stop failed: {exc}") from exc
    return dict(result)


def copy_trace(stop_result: Mapping[str, Any], destination_dir: Path) -> Path | None:
    """Copy the recording's trace file next to the level results, if it exists."""
    trace_path = stop_result.get("trace_path")
    if not isinstance(trace_path, str) or not trace_path:
        return None
    source = Path(trace_path)
    if not source.is_file():
        return None
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    shutil.copy2(source, destination)
    return destination


def digest_recording(stop_result: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a stop result to the figures the report and comparison use."""
    summary = _mapping(stop_result.get("summary"))
    metrics = _mapping(summary.get("metrics"))
    gauges = _mapping(summary.get("gauges_max"))
    raw_stalls = summary.get("stalls")
    stalls = raw_stalls if isinstance(raw_stalls, list) else []

    return {
        "recording_id": stop_result.get("recording_id"),
        "duration_seconds": stop_result.get("duration_seconds"),
        "event_count": stop_result.get("event_count"),
        "truncated": stop_result.get("truncated"),
        "stopped_reason": stop_result.get("stopped_reason"),
        **{key: _metric(metrics, name) for key, name in DIGEST_METRICS.items()},
        **{key: gauges.get(name) for key, name in DIGEST_GAUGES.items()},
        "tools_ms": {
            name[len(_TOOL_PREFIX) :]: _metric(metrics, name)
            for name in sorted(metrics)
            if name.startswith(_TOOL_PREFIX)
        },
        "worker_pools": _worker_pools(metrics, gauges),
        "top_metrics": _top_metrics(metrics),
        "stalls": _stalls(stalls),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _metric(metrics: Mapping[str, Any], name: str) -> dict[str, Any]:
    values = metrics.get(name)
    if not isinstance(values, dict):
        return {"count": 0, "p50": None, "p90": None, "p99": None, "max": None, "sum": None}
    return {
        "count": values.get("count", 0),
        "p50": values.get("p50_ms"),
        "p90": values.get("p90_ms"),
        "p99": values.get("p99_ms"),
        "max": values.get("max_ms"),
        "sum": values.get("sum_ms"),
    }


def _worker_pools(metrics: Mapping[str, Any], gauges: Mapping[str, Any]) -> list[dict[str, Any]]:
    pools = []
    for name in metrics:
        if not (name.startswith(_WORKER_POOL_PREFIX) and name.endswith(".wait")):
            continue
        pool = name[len(_WORKER_POOL_PREFIX) : -len(".wait")]
        wait = _metric(metrics, name)
        run = _metric(metrics, f"{_WORKER_POOL_PREFIX}{pool}.run")
        pools.append(
            {
                "pool": pool,
                "wait_count": wait["count"],
                "wait_p99_ms": wait["p99"],
                "wait_max_ms": wait["max"],
                "run_p99_ms": run["p99"],
                "active_max": gauges.get(f"{_WORKER_POOL_PREFIX}{pool}.active"),
                "waiting_max": gauges.get(f"{_WORKER_POOL_PREFIX}{pool}.waiting"),
            }
        )
    pools.sort(key=lambda pool: pool["wait_p99_ms"] or 0.0, reverse=True)
    return pools[:TOP_WORKER_POOLS]


def _top_metrics(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {"name": name, **_metric(metrics, name)}
        for name in metrics
        if isinstance(metrics.get(name), dict)
    ]
    rows.sort(key=lambda row: row["sum"] or 0.0, reverse=True)
    return rows[:TOP_METRICS]


def _stalls(stalls: list[Any]) -> dict[str, Any]:
    valid = [stall for stall in stalls if isinstance(stall, dict)]
    durations = [float(stall.get("duration_ms") or 0.0) for stall in valid]
    worst = max(valid, key=lambda stall: float(stall.get("duration_ms") or 0.0), default=None)
    worst_digest = None
    if worst is not None:
        samples = [sample for sample in worst.get("samples") or [] if isinstance(sample, dict)]
        samples.sort(key=lambda sample: int(sample.get("count") or 0), reverse=True)
        top = samples[0] if samples else {}
        stack = [str(frame) for frame in top.get("stack") or []]
        worst_digest = {
            "started_at": worst.get("started_at"),
            "duration_ms": worst.get("duration_ms"),
            "sample_count": top.get("count"),
            # vBot renders stall stacks innermost frame first.
            "frames": stack[:STALL_FRAMES],
        }
    return {
        "count": len(valid),
        "total_ms": round(sum(durations), 3),
        "worst": worst_digest,
    }
