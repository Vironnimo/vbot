"""Performance measurement RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from cli.formatting import bool_text as _bool_text
from cli.formatting import string_or_default as _string_or_default
from cli.formatting import value_text as _value_text
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance
from core.performance import MAX_HISTORY_WINDOWS

PERFETTO_URL = "https://ui.perfetto.dev"
_TOP_METRICS = 10
_SUMMARY_METRICS = 5
_RECENT_STALLS = 5
_STALL_FRAMES = 3
_STALL_THREADS = 2
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
_HISTORY_NAMES = (
    "process.rss_mb",
    "process.cpu_percent",
    "runs.active",
    "event_loop.lag",
    "gc.gen2",
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
        *_counter_section(_mapping(payload.data.get("counters"))),
        *_stall_section(stalls, f"recent stalls (newest first, {len(stalls)} retained):"),
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


def performance_heap(instance: ServerInstance, top: int) -> CommandResult:
    """Summarize a garbage collector census via `performance.heap` RPC."""

    payload = _rpc_call(instance, "performance.heap", {"top": top})
    if not payload.ok:
        return payload.to_command_result()
    data = payload.data
    generations = [entry for entry in _sequence(data.get("generations")) if isinstance(entry, dict)]
    growth = _sequence(data.get("growth"))
    previous = data.get("previous_taken_at")
    lines = [
        f"heap census: taken_at={_string_or_default(data.get('taken_at'), '-')} "
        f"duration_ms={_value_text(data.get('duration_ms'))} "
        f"tracked={_value_text(data.get('tracked'))} frozen={_value_text(data.get('frozen'))}",
        "generations: "
        + (
            " | ".join(
                f"gen{index} objects={_value_text(entry.get('objects'))} "
                f"collections={_value_text(entry.get('collections'))}"
                for index, entry in enumerate(generations)
            )
            or "-"
        ),
        *_census_section("top types", _sequence(data.get("types"))),
        *_census_section("top modules", _sequence(data.get("modules"))),
    ]
    if previous is None:
        lines.append("growth: first census of this server process; run it again later to compare")
    else:
        lines.extend(
            _census_section(f"largest growth since {_string_or_default(previous, '-')}", growth)
        )
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def performance_history(
    instance: ServerInstance, hours: int, at: str | None, metrics: Sequence[str]
) -> CommandResult:
    """Show stored window summaries via `performance.history` RPC.

    Without options it prints one health line per window of the last ``hours``;
    ``metrics`` prints those metrics per window instead, and ``at`` prints the
    full windows containing that time (local time unless it has an offset).
    """

    params: dict[str, Any] = {"limit": MAX_HISTORY_WINDOWS}
    if at is not None:
        moment = _parse_local_time(at)
        if moment is None:
            return CommandResult(
                ok=False,
                message=f"invalid --at time: {at} (expected e.g. 2026-09-28 16:40)",
                instance=instance,
            )
        params["since"] = params["until"] = moment.isoformat()
    else:
        params["since"] = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        params["names"] = list(metrics or _HISTORY_NAMES)
    payload = _rpc_call(instance, "performance.history", params)
    if not payload.ok:
        return payload.to_command_result()
    windows = [entry for entry in _sequence(payload.data.get("windows")) if isinstance(entry, dict)]
    if not windows:
        return CommandResult(
            ok=True, message="no performance history stored for this period", instance=instance
        )
    lines = [
        f"performance history: {len(windows)} windows of "
        f"{_value_text(payload.data.get('interval_seconds'))} s, oldest first, "
        f"times in local time (UTC{datetime.now().astimezone().strftime('%z')})"
    ]
    previous_process: object = None
    for window in windows:
        process = window.get("process_started_at")
        if process != previous_process:
            lines.append(f"server started {_local_time(process)}")
            previous_process = process
        if at is not None:
            lines.extend(_window_details(window))
        elif metrics:
            lines.extend(_window_series(window, metrics))
        else:
            lines.append(_window_health(window))
    return CommandResult(ok=True, message="\n".join(lines), instance=instance)


def _window_health(window: Mapping[str, Any]) -> str:
    gauges_max = _mapping(window.get("gauges_max"))
    metrics = _mapping(window.get("metrics"))
    lag = _mapping(metrics.get("event_loop.lag"))
    gen2 = _mapping(metrics.get("gc.gen2"))
    return (
        f"- {_window_span(window)} "
        f"rss_mb_max={_value_text(gauges_max.get('process.rss_mb'))} "
        f"cpu_percent_max={_value_text(gauges_max.get('process.cpu_percent'))} "
        f"runs_active_max={_value_text(gauges_max.get('runs.active'))} "
        f"lag_p99_ms={_value_text(lag.get('p99_ms'))} "
        f"gc_gen2={_value_text(gen2.get('count', 0))}x max_ms={_value_text(gen2.get('max_ms'))} "
        f"stalls={len(_sequence(window.get('stalls')))}"
    )


def _window_series(window: Mapping[str, Any], names: Sequence[str]) -> list[str]:
    metrics = _mapping(window.get("metrics"))
    counters = _mapping(window.get("counters"))
    gauges = _mapping(window.get("gauges"))
    gauges_max = _mapping(window.get("gauges_max"))
    lines: list[str] = []
    for name in names:
        if isinstance(metrics.get(name), dict):
            row = _metric_row(name, metrics[name]).removeprefix("- ")
        elif name in counters:
            row = f"{name}=+{_value_text(counters[name])}"
        elif name in gauges or name in gauges_max:
            row = (
                f"{name} last={_value_text(gauges.get(name))} "
                f"max={_value_text(gauges_max.get(name))}"
            )
        else:
            row = f"{name} -"
        lines.append(f"- {_window_span(window)} {row}")
    return lines


def _window_details(window: Mapping[str, Any]) -> list[str]:
    metrics = _mapping(window.get("metrics"))
    gauges_max = _mapping(window.get("gauges_max"))
    stalls = _sequence(window.get("stalls"))
    dropped = window.get("stalls_dropped")
    extra = f", {dropped} more not stored" if isinstance(dropped, int) and dropped else ""
    return [
        f"window {_window_span(window)} reason={_string_or_default(window.get('reason'), '-')}",
        _gauge_line(gauges_max, "gauges max"),
        *_metric_section("top metrics by p99", metrics, "p99_ms", _TOP_METRICS),
        *_metric_section("top metrics by total time", metrics, "sum_ms", _TOP_METRICS),
        *_counter_section(_mapping(window.get("counters"))),
        *_stall_section(stalls, f"stalls (newest first, {len(stalls)} stored{extra}):"),
    ]


def _window_span(window: Mapping[str, Any]) -> str:
    started = _local_datetime(window.get("started_at"))
    ended = _local_datetime(window.get("ended_at"))
    if started is None or ended is None:
        return "?"
    return f"{started:%Y-%m-%d %H:%M}-{ended:%H:%M}"


def _local_time(value: object) -> str:
    moment = _local_datetime(value)
    return "?" if moment is None else f"{moment:%Y-%m-%d %H:%M:%S}"


def _local_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def _parse_local_time(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    # Log lines carry local time without an offset; read such a time the same way.
    return moment.astimezone() if moment.tzinfo is None else moment


def _census_section(title: str, rows: Sequence[object]) -> list[str]:
    entries = [row for row in rows if isinstance(row, dict)]
    if not entries:
        return [f"{title}: -"]
    lines = [f"{title}:"]
    for row in entries:
        change = row.get("change")
        change_text = "" if not isinstance(change, int) else f" change={change:+d}"
        lines.append(
            f"- {_string_or_default(row.get('name'), '?')} "
            f"count={_value_text(row.get('count'))}{change_text}"
        )
    return lines


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


def _gauge_line(gauges: Mapping[str, Any], title: str = "gauges") -> str:
    fields = [f"{name}={_value_text(gauges[name])}" for name in _KEY_GAUGES if name in gauges]
    return f"{title}: " + (" ".join(fields) if fields else "-")


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


def _counter_section(counters: Mapping[str, Any]) -> list[str]:
    rows = [(name, value) for name, value in counters.items() if isinstance(value, int)]
    if not rows:
        return ["top counters: -"]
    rows.sort(key=lambda row: (-row[1], row[0]))
    return ["top counters:", *(f"- {name}={value}" for name, value in rows[:_TOP_METRICS])]


def _metric_row(name: str, values: Mapping[str, Any]) -> str:
    return (
        f"- {name} count={_value_text(values.get('count'))} "
        f"p50_ms={_value_text(values.get('p50_ms'))} p90_ms={_value_text(values.get('p90_ms'))} "
        f"p99_ms={_value_text(values.get('p99_ms'))} max_ms={_value_text(values.get('max_ms'))} "
        f"sum_ms={_value_text(values.get('sum_ms'))}"
    )


def _stall_section(stalls: Sequence[object], title: str) -> list[str]:
    if not stalls:
        return [f"{title.split(' (', 1)[0]}: none"]
    lines = [title]
    for stall in list(reversed(stalls))[:_RECENT_STALLS]:
        if not isinstance(stall, dict):
            lines.append("- invalid stall entry")
            continue
        samples = [sample for sample in stall.get("samples") or [] if isinstance(sample, dict)]
        window = stall.get("cpu_window_ms")
        lines.append(
            f"- started_at={_string_or_default(stall.get('started_at'), '-')} "
            f"duration_ms={_value_text(stall.get('duration_ms'))} "
            f"gc_ms={_value_text(stall.get('gc_ms'))} "
            f"loop_cpu_ms={_cpu_share(stall.get('loop_cpu_ms'), window)} "
            f"samples={sum(_count(sample) for sample in samples)}"
        )
        lines.extend(_common_stack(samples))
        threads = [thread for thread in _sequence(stall.get("threads")) if isinstance(thread, dict)]
        for thread in threads[:_STALL_THREADS]:
            lines.append(
                f"  thread {_string_or_default(thread.get('name'), '?')} "
                f"cpu_ms={_cpu_share(thread.get('cpu_ms'), window)}"
            )
            lines.extend(
                _common_stack(
                    [
                        sample
                        for sample in _sequence(thread.get("samples"))
                        if isinstance(sample, dict)
                    ]
                )
            )
    return lines


def _common_stack(samples: Sequence[Mapping[str, Any]]) -> list[str]:
    if not samples:
        return []
    stack = _sequence(max(samples, key=_count).get("stack"))
    return [f"    {frame}" for frame in stack[:_STALL_FRAMES]]


def _cpu_share(cpu_ms: object, window_ms: object) -> str:
    if cpu_ms is None:
        return "-"
    return f"{_value_text(cpu_ms)}/{_value_text(window_ms)}"


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
