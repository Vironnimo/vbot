"""Performance measurement RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.formatting import bool_text as _bool_text
from cli.formatting import string_or_default as _string_or_default
from cli.formatting import value_text as _value_text
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

PERFETTO_URL = "https://ui.perfetto.dev"
_TOP_METRICS = 10
_SUMMARY_METRICS = 5
_RECENT_STALLS = 5
_STALL_FRAMES = 3
_KEY_GAUGES = (
    "event_loop.utilization",
    "process.cpu_percent",
    "process.rss_mb",
    "process.python_threads",
    "asyncio.tasks",
    "runs.active",
    "runs.queued",
    "performance.dropped_metrics",
)


def performance_status(instance: ServerInstance) -> CommandResult:
    """Summarize the `performance.snapshot` RPC result."""

    payload = _rpc_call(instance, "performance.snapshot", {})
    if not payload.ok:
        return payload.to_command_result()
    metrics = payload.data.get("metrics")
    gauges = payload.data.get("gauges")
    stalls = payload.data.get("stalls")
    if (
        not isinstance(metrics, dict)
        or not isinstance(gauges, dict)
        or not isinstance(stalls, list)
    ):
        return CommandResult(
            ok=False, message="RPC result missing performance snapshot fields", instance=instance
        )
    lines = [
        f"started_at={_string_or_default(payload.data.get('started_at'), '-')} "
        f"uptime_seconds={_value_text(payload.data.get('uptime_seconds'))}",
        _recording_line(payload.data.get("recording")),
        _gauge_line(gauges),
        *_metric_section("top metrics by p99", metrics, "p99_ms", _TOP_METRICS),
        *_metric_section("top metrics by total time", metrics, "sum_ms", _TOP_METRICS),
        *_stall_section(stalls),
    ]
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def performance_record_start(
    instance: ServerInstance, label: str | None, max_seconds: int
) -> CommandResult:
    """Start a recording via `performance.recording_start` RPC."""

    params: dict[str, Any] = {"max_seconds": max_seconds}
    if label is not None:
        params["label"] = label
    payload = _rpc_call(instance, "performance.recording_start", params)
    if not payload.ok:
        return payload.to_command_result()
    recording_id = _string_or_default(payload.data.get("recording_id"), "?")
    return CommandResult(
        ok=True,
        message=(
            f"recording started: id={recording_id} "
            f"label={_string_or_default(payload.data.get('label'), '-')} "
            f"max_seconds={_value_text(payload.data.get('max_seconds'))}\n"
            "stop it with: vbot performance record stop (keep the same target options)"
        ),
        instance=instance,
    )


def performance_record_stop(instance: ServerInstance) -> CommandResult:
    """Stop the active recording via `performance.recording_stop` RPC."""

    payload = _rpc_call(instance, "performance.recording_stop", {})
    if not payload.ok:
        return payload.to_command_result()
    data = payload.data
    summary = _mapping(data.get("summary"))
    metrics = _mapping(summary.get("metrics"))
    stalls = _sequence(summary.get("stalls"))
    lines = [
        f"recording stopped: id={_string_or_default(data.get('recording_id'), '?')} "
        f"reason={_string_or_default(data.get('stopped_reason'), '-')} "
        f"duration_seconds={_value_text(data.get('duration_seconds'))} "
        f"events={_value_text(data.get('event_count'))} "
        f"truncated={_bool_text(data.get('truncated'))} "
        f"stalls={len(stalls)}",
        f"trace: {_string_or_default(data.get('trace_path'), '-')}",
        f"open the trace file at {PERFETTO_URL}",
        *_metric_section("top metrics by p99", metrics, "p99_ms", _SUMMARY_METRICS),
    ]
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def performance_recordings(instance: ServerInstance, limit: int) -> CommandResult:
    """List stored recordings via `performance.recording_list` RPC."""

    payload = _rpc_call(instance, "performance.recording_list", {"limit": limit})
    if not payload.ok:
        return payload.to_command_result()
    recordings = payload.data.get("recordings")
    if not isinstance(recordings, list):
        return CommandResult(
            ok=False, message="RPC result missing recordings list", instance=instance
        )
    if not recordings:
        return CommandResult(ok=True, message="no performance recordings stored", instance=instance)
    lines = ["recordings:", *(_recording_row(entry) for entry in recordings)]
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def _recording_line(recording: object) -> str:
    if not isinstance(recording, dict):
        return "recording: none"
    return (
        f"recording: id={_string_or_default(recording.get('recording_id'), '?')} "
        f"label={_string_or_default(recording.get('label'), '-')} "
        f"elapsed_seconds={_value_text(recording.get('elapsed_seconds'))} "
        f"max_seconds={_value_text(recording.get('max_seconds'))} "
        f"events={_value_text(recording.get('event_count'))} "
        f"truncated={_bool_text(recording.get('truncated'))}"
    )


def _gauge_line(gauges: Mapping[str, Any]) -> str:
    fields = [f"{name}={_value_text(gauges[name])}" for name in _KEY_GAUGES if name in gauges]
    return "gauges: " + (" ".join(fields) if fields else "-")


def _metric_section(title: str, metrics: Mapping[str, Any], key: str, limit: int) -> list[str]:
    rows = [
        (name, values)
        for name, values in metrics.items()
        if isinstance(values, dict) and isinstance(values.get(key), int | float)
    ]
    if not rows:
        return [f"{title}: -"]
    rows.sort(key=lambda row: (-row[1][key], row[0]))
    return [f"{title}:", *(_metric_row(name, values) for name, values in rows[:limit])]


def _metric_row(name: str, values: Mapping[str, Any]) -> str:
    return (
        f"- {name} count={_value_text(values.get('count'))} "
        f"p50_ms={_value_text(values.get('p50_ms'))} p90_ms={_value_text(values.get('p90_ms'))} "
        f"p99_ms={_value_text(values.get('p99_ms'))} max_ms={_value_text(values.get('max_ms'))} "
        f"sum_ms={_value_text(values.get('sum_ms'))}"
    )


def _stall_section(stalls: Sequence[object]) -> list[str]:
    if not stalls:
        return ["recent stalls: none"]
    lines = [f"recent stalls (newest first, {len(stalls)} retained):"]
    for stall in list(reversed(stalls))[:_RECENT_STALLS]:
        if not isinstance(stall, dict):
            lines.append("- invalid stall entry")
            continue
        samples = [sample for sample in stall.get("samples") or [] if isinstance(sample, dict)]
        lines.append(
            f"- started_at={_string_or_default(stall.get('started_at'), '-')} "
            f"duration_ms={_value_text(stall.get('duration_ms'))} "
            f"samples={sum(_count(sample) for sample in samples)}"
        )
        if samples:
            common = max(samples, key=_count)
            stack = _sequence(common.get("stack"))
            lines.extend(f"    {frame}" for frame in stack[:_STALL_FRAMES])
    return lines


def _recording_row(entry: object) -> str:
    if not isinstance(entry, dict):
        return "- invalid recording entry"
    return (
        f"- id={_string_or_default(entry.get('recording_id'), '?')} "
        f"started_at={_string_or_default(entry.get('started_at'), '-')} "
        f"duration_seconds={_value_text(entry.get('duration_seconds'))} "
        f"events={_value_text(entry.get('event_count'))} "
        f"truncated={_bool_text(entry.get('truncated'))} "
        f"reason={_string_or_default(entry.get('stopped_reason'), '-')} "
        f"label={_string_or_default(entry.get('label'), '-')}\n"
        f"  trace={_string_or_default(entry.get('trace_path'), '-')}"
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def _count(sample: Mapping[str, Any]) -> int:
    count = sample.get("count")
    return count if isinstance(count, int) else 0
