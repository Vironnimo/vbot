"""``result.json`` shape, console and Markdown reports, and run comparison.

A result without ``scenario`` comes from the sessions scenario; rows whose
figures a level does not have are left out of the tables.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

RESULT_KIND = "vbot-perf-load"
RESULT_SCHEMA = 1
DEFAULT_SCENARIO = "sessions"
TOP_REPORTED_RPC_METHODS = 12
TOP_REPORTED_COUNTERS = 25


def _p50_p99_max(prefix: str) -> tuple[str, str, str]:
    return (f"{prefix}.p50", f"{prefix}.p99", f"{prefix}.max")


# (label, dotted paths into one level). Several paths render as "a / b / c".
TABLE_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Runs ok / total", ("client.runs.ok", "client.runs.total")),
    ("Swarm participants / Runs", ("swarm.participants", "swarm.participant_runs")),
    ("Swarm turns scripted / idle", ("swarm.turns.scripted", "swarm.turns.idle")),
    (
        "Board posts total / participants / kicks",
        ("swarm.board_posts", "swarm.participant_posts", "swarm.kicks"),
    ),
    ("Tool calls / Tool errors", ("client.runs.tool_calls", "client.runs.tool_errors")),
    ("TTFT p50 / p95 ms", ("client.ttft_ms.p50", "client.ttft_ms.p95")),
    ("TTFT overhead p50 / p95 ms", ("client.ttft_overhead_ms.p50", "client.ttft_overhead_ms.p95")),
    ("Send->1st request p50 / p95 ms", ("client.admission_ms.p50", "client.admission_ms.p95")),
    ("Step overhead p50 / p95 ms", ("client.step_overhead_ms.p50", "client.step_overhead_ms.p95")),
    (
        "Step excl. Tool exec p50 / p95 ms",
        ("client.step_overhead_excl_tool_ms.p50", "client.step_overhead_excl_tool_ms.p95"),
    ),
    (
        "Delta latency p50 / p95 / p99 ms",
        (
            "client.delta_latency_ms.p50",
            "client.delta_latency_ms.p95",
            "client.delta_latency_ms.p99",
        ),
    ),
    ("Run duration p50 / ideal ms", ("client.run_duration_ms.p50", "client.run_ideal_ms.p50")),
    ("Run excess p50 / p95 ms", ("client.run_excess_ms.p50", "client.run_excess_ms.p95")),
    ("Run duration / ideal p50", ("client.run_duration_ratio.p50",)),
    (
        "Start->1st request p50 / max ms",
        ("client.start_to_first_request_ms.p50", "client.start_to_first_request_ms.max"),
    ),
    ("Turn duration p50 / ideal ms", ("client.turn_duration_ms.p50", "client.turn_ideal_ms")),
    ("Turn excess p50 / p95 ms", ("client.turn_excess_ms.p50", "client.turn_excess_ms.p95")),
    ("Server CPU avg / max %", ("server_process.cpu_avg", "server_process.cpu_max")),
    ("Server tree CPU avg / max %", ("server_process.tree_cpu_avg", "server_process.tree_cpu_max")),
    ("Server RSS max MB", ("server_process.rss_max_mb",)),
    (
        "Event loop lag p50 / p99 / max ms",
        (
            "server.event_loop_lag_ms.p50",
            "server.event_loop_lag_ms.p99",
            "server.event_loop_lag_ms.max",
        ),
    ),
    ("Event loop utilization max", ("server.event_loop_utilization_max",)),
    ("Stalls count / worst ms", ("server.stalls.count", "server.stalls.worst.duration_ms")),
    ("Runs active / queued max", ("server.runs_active_max", "server.runs_queued_max")),
    ("sqlite.sessions.write p50 / p99 / max ms", _p50_p99_max("server.sqlite_write_ms")),
    ("sqlite.sessions.write_wait p50 / p99 / max ms", _p50_p99_max("server.sqlite_write_wait_ms")),
    ("sqlite.sessions.read p50 / p99 / max ms", _p50_p99_max("server.sqlite_read_ms")),
    ("chat.request_build p50 / p99 / max ms", _p50_p99_max("server.chat_request_build_ms")),
    ("chat.persist p50 / p99 / max ms", _p50_p99_max("server.chat_persist_ms")),
    ("chat.tool_round p50 / p99 / max ms", _p50_p99_max("server.chat_tool_round_ms")),
    (
        "Fake Provider CPU avg / max %",
        ("provider_process.tree_cpu_avg", "provider_process.tree_cpu_max"),
    ),
    ("Fake Provider max emit lag ms", ("client.provider.max_emit_lag_ms",)),
    (
        "Aux / retried Provider requests",
        ("client.provider.aux_requests", "client.provider.retried_requests"),
    ),
    ("UI long tasks count / max ms", ("ui.long_tasks.count", "ui.long_tasks.max_ms")),
    ("UI frame gaps >50ms count / max", ("ui.frame_gaps.count", "ui.frame_gaps.max_ms")),
    (
        "UI page frame gaps >50ms count / max",
        ("ui.page_frame_gaps.count", "ui.page_frame_gaps.max_ms"),
    ),
    (
        "UI marker latency p50 / p95 ms",
        ("ui.dom_marker_latency_ms.p50", "ui.dom_marker_latency_ms.p95"),
    ),
    ("UI RPC calls / per s", ("ui.rpc.count", "ui.rpc.per_second")),
    (
        "RSS first / last / max MB",
        (
            "timeline.trends.rss_mb.first",
            "timeline.trends.rss_mb.last",
            "timeline.trends.rss_mb.max",
        ),
    ),
    (
        "asyncio Tasks first / last / max",
        (
            "timeline.trends.asyncio_tasks.first",
            "timeline.trends.asyncio_tasks.last",
            "timeline.trends.asyncio_tasks.max",
        ),
    ),
    (
        "gc.gen2 collections first / last",
        ("timeline.trends.gc_gen2_count.first", "timeline.trends.gc_gen2_count.last"),
    ),
    ("gc.gen2 max pause ms", ("timeline.trends.gc_gen2_max_ms.last",)),
    ("Heap tracked start / end", ("heap.tracked_start", "heap.tracked_end")),
)

# Scalar metrics compared between two result files, all "lower is better"
# except where noted in HIGHER_IS_BETTER.
COMPARE_KEYS: tuple[str, ...] = (
    "client.runs.ok",
    "client.runs.failed",
    "client.ttft_overhead_ms.p50",
    "client.ttft_overhead_ms.p95",
    "client.admission_ms.p50",
    "client.admission_ms.p95",
    "client.step_overhead_ms.p50",
    "client.step_overhead_ms.p95",
    "client.step_overhead_excl_tool_ms.p50",
    "client.step_overhead_excl_tool_ms.p95",
    "client.delta_latency_ms.p50",
    "client.delta_latency_ms.p95",
    "client.delta_latency_ms.p99",
    "client.run_duration_ms.p50",
    "client.run_excess_ms.p50",
    "client.run_excess_ms.p95",
    "server_process.cpu_avg",
    "server_process.cpu_max",
    "server_process.rss_max_mb",
    "server.event_loop_lag_ms.p50",
    "server.event_loop_lag_ms.p99",
    "server.event_loop_lag_ms.max",
    "server.event_loop_utilization_max",
    "server.sqlite_write_ms.p99",
    "server.sqlite_write_ms.max",
    "server.sqlite_write_wait_ms.p99",
    "server.sqlite_read_ms.p99",
    "server.chat_request_build_ms.p50",
    "server.chat_request_build_ms.p99",
    "server.chat_persist_ms.p50",
    "server.chat_persist_ms.p99",
    "server.chat_tool_round_ms.p50",
    "server.chat_tool_round_ms.p99",
    "server.stalls.count",
    "ui.long_tasks.count",
    "ui.long_tasks.max_ms",
    "ui.frame_gaps.count",
    "ui.frame_gaps.max_ms",
    "ui.rpc.count",
    "client.turn_duration_ms.p50",
    "client.turn_excess_ms.p50",
    "client.turn_excess_ms.p95",
    "client.start_to_first_request_ms.p50",
    "timeline.trends.rss_mb.change",
    "timeline.trends.asyncio_tasks.change",
    "heap.tracked_change",
)
HIGHER_IS_BETTER = frozenset({"client.runs.ok"})


def get_path(data: Any, dotted: str) -> Any:
    """Return ``data[a][b][c]`` for ``"a.b.c"``; ``None`` when any step is missing."""
    current = data
    for key in dotted.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if abs(value) >= 100:
            return f"{value:.0f}"
        if abs(value) >= 10:
            return f"{value:.1f}"
        return f"{value:.2f}"
    return str(value)


def scenario_of(data: Mapping[str, Any]) -> str:
    """The scenario of a result or level; older results are sessions runs."""
    scenario = data.get("scenario")
    return scenario if isinstance(scenario, str) else DEFAULT_SCENARIO


def level_label(level: Mapping[str, Any]) -> str:
    agents = level.get("agents")
    return f"{agents} agent" if agents == 1 else f"{agents} agents"


def table_rows(levels: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    """Metric rows (label + one cell per level) shared by console and Markdown."""
    rows = []
    for label, paths in TABLE_ROWS:
        cells = [
            " / ".join(format_value(get_path(level, path)) for path in paths) for level in levels
        ]
        if all(set(cell) <= {"-", " ", "/"} for cell in cells):
            continue
        rows.append([label, *cells])
    return rows


def render_console(result: Mapping[str, Any]) -> str:
    levels = list(result.get("levels") or [])
    header = ["Metric", *(level_label(level) for level in levels)]
    rows = [header, *table_rows(levels)]
    for level in levels:
        if level.get("status") != "ok":
            rows.append([f"{level_label(level)} FAILED", str(level.get("error") or "")[:60]])
    widths = [
        max(len(row[index]) for row in rows if index < len(row)) for index in range(len(header))
    ]
    lines = []
    for position, row in enumerate(rows):
        lines.append(
            "  ".join(
                cell.ljust(widths[index]) if index == 0 else cell.rjust(widths[index])
                for index, cell in enumerate(row)
            ).rstrip()
        )
        if position == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def _markdown_table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return lines


def render_markdown(result: Mapping[str, Any]) -> str:
    levels = list(result.get("levels") or [])
    config = result.get("config") or {}
    machine = result.get("machine") or {}
    git = result.get("git") or {}
    scenario = scenario_of(result)
    duration = config.get("duration_minutes")
    per = "participant" if scenario == "swarm" else "Session"
    shape = (
        f"{duration} min of turns per {per}"
        if duration is not None
        else f"{config.get('turns')} turn(s) per {per}"
    )
    lines = [
        "# vBot load report",
        "",
        f"- Started: {result.get('started_at')} (finished {result.get('finished_at')})",
        f"- Checkout: {git.get('commit')} on {git.get('branch')}"
        + (" (uncommitted changes)" if git.get("dirty") else ""),
        f"- Machine: {machine.get('platform')}, Python {machine.get('python')}, "
        f"{machine.get('cpu_logical')} logical CPUs, {machine.get('memory_gb')} GB RAM",
        f"- Scenario: {scenario}, {shape}, steps={config.get('steps')}, "
        f"tokens={config.get('tokens')}, rate={config.get('rate')}/s, "
        f"think_ms={config.get('think_ms')}, tools={','.join(config.get('tools') or [])}, "
        f"calls={config.get('calls')}, history_tokens={config.get('history_tokens')}",
        "",
        "Definitions: *TTFT* is send -> first `assistant_output_delta`; *TTFT overhead* subtracts "
        "`think_ms` and therefore still contains every Tool round of the turn. *Step overhead* "
        "is the gap between the fake Provider finishing a Tool-call response and receiving the "
        "next request (Tool execution + persistence + request build + scheduling); *excl. Tool "
        "exec* subtracts vBot's own Tool duration. *Delta latency* is marker emission -> SSE "
        "receipt (includes vBot's 40 ms delta batching). *Ideal* Run duration is the scripted "
        "Provider time (think + tokens / rate).",
        "",
    ]
    if scenario == "swarm":
        lines.extend(
            [
                "Swarm: one Swarm per level with one participant per agent. A *turn* is one "
                "scripted round trip of a participant (Tool rounds, then text); turn 1 starts "
                "with reading the goal post. *Turn duration* runs from the turn's first Model "
                "request to its final text as the fake Provider sees them; *turn excess* "
                "subtracts the scripted text time. *Idle* turns are plain answers of "
                "participants without work left; *kicks* are harness posts that wake a Swarm "
                "that fell idle with turns left. Participant Runs are counted from their "
                "histories.",
                "",
            ]
        )
    if any(level.get("ui") for level in levels):
        lines.extend(
            [
                "UI: *RPC calls* are the `/api/rpc` requests the browser made while measuring, "
                "including Extension page frames; *page frame gaps* come from Extension frames.",
                "",
            ]
        )
    lines.extend(["## Summary", ""])
    header = ["Metric", *(level_label(level) for level in levels)]
    lines.extend(_markdown_table(header, table_rows(levels)))
    for level in levels:
        lines.extend(["", f"## {level_label(level)}", ""])
        lines.extend(_level_details(level))
    # Sections separate themselves with blank lines; keep at most one in a row.
    collapsed = [line for index, line in enumerate(lines) if line or index == 0 or lines[index - 1]]
    return "\n".join(collapsed).rstrip("\n") + "\n"


def _level_details(level: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    if level.get("status") != "ok":
        lines.extend([f"**Failed:** {level.get('error')}", ""])
    lines.extend(f"- Note: {note}" for note in level.get("notes") or [])
    by_tool = get_path(level, "client.step_overhead_by_tool_ms") or {}
    if by_tool:
        lines.extend(["", "Step overhead by Tool round (ms):", ""])
        rows = [
            [
                tools,
                format_value(get_path(values, "gap.count")),
                format_value(get_path(values, "gap.p50")),
                format_value(get_path(values, "gap.p95")),
                format_value(get_path(values, "tool_exec.p50")),
                format_value(get_path(values, "tool_exec.p95")),
            ]
            for tools, values in by_tool.items()
        ]
        lines.extend(
            _markdown_table(
                ["Tools", "Rounds", "Gap p50", "Gap p95", "Tool exec p50", "Tool exec p95"], rows
            )
        )
    server = level.get("server") or {}
    pools = server.get("worker_pools") or []
    if pools:
        lines.extend(["", "Worker pools by wait p99:", ""])
        rows = [
            [
                str(pool.get("pool")),
                format_value(pool.get("wait_count")),
                format_value(pool.get("wait_p99_ms")),
                format_value(pool.get("wait_max_ms")),
                format_value(pool.get("run_p99_ms")),
                format_value(pool.get("active_max")),
                format_value(pool.get("waiting_max")),
            ]
            for pool in pools
        ]
        header = [
            "Pool",
            "Waits",
            "Wait p99 ms",
            "Wait max ms",
            "Run p99 ms",
            "Active max",
            "Waiting max",
        ]
        lines.extend(_markdown_table(header, rows))
    tools = server.get("tools_ms") or {}
    if tools:
        lines.extend(["", "Tool dispatch measured by the server (`tool.<name>`):", ""])
        rows = [
            [
                name,
                format_value(values.get("count")),
                format_value(values.get("p50")),
                format_value(values.get("p99")),
                format_value(values.get("max")),
            ]
            for name, values in tools.items()
        ]
        lines.extend(_markdown_table(["Tool", "Count", "p50 ms", "p99 ms", "Max ms"], rows))
    top = server.get("top_metrics") or []
    if top:
        lines.extend(["", "Server metrics by total time:", ""])
        rows = [
            [
                str(metric.get("name")),
                format_value(metric.get("count")),
                format_value(metric.get("sum")),
                format_value(metric.get("p50")),
                format_value(metric.get("p99")),
                format_value(metric.get("max")),
            ]
            for metric in top
        ]
        lines.extend(
            _markdown_table(["Metric", "Count", "Total ms", "p50 ms", "p99 ms", "Max ms"], rows)
        )
    lines.extend(_swarm_details(level))
    lines.extend(_rpc_details(level))
    lines.extend(_counter_details(level))
    lines.extend(_timeline_details(level))
    lines.extend(_heap_details(level))
    worst = get_path(level, "server.stalls.worst")
    if worst:
        lines.extend(
            [
                "",
                f"Worst Event Loop stall: {format_value(worst.get('duration_ms'))} ms at "
                f"{worst.get('started_at')} (most frequent sampled stack, innermost frame first):",
                "",
                "```",
                *[str(frame) for frame in worst.get("frames") or []],
                "```",
            ]
        )
    bullets = []
    profile = level.get("profile")
    if profile:
        bullets.append(f"- Profile: {json.dumps(profile, sort_keys=True)}")
    ui = level.get("ui")
    if ui and ui.get("status") != "ok":
        bullets.append(f"- UI probe {ui.get('status')}: {ui.get('reason')}")
    files = level.get("files") or {}
    bullets.extend(f"- File `{name}`: `{path}`" for name, path in sorted(files.items()))
    if bullets:
        lines.extend(["", *bullets])
    errors = get_path(level, "client.errors") or []
    if errors:
        lines.extend(["", "Run errors (first distinct):", ""])
        lines.extend(f"- {error}" for error in errors)
    return lines


def _swarm_details(level: Mapping[str, Any]) -> list[str]:
    by_name = get_path(level, "swarm.tool_calls_by_name") or {}
    if not by_name:
        return []
    lines = ["", "Participant Tool calls:", ""]
    rows = [[name, format_value(count)] for name, count in by_name.items()]
    lines.extend(_markdown_table(["Tool", "Calls"], rows))
    return lines


def _rpc_details(level: Mapping[str, Any]) -> list[str]:
    calls = get_path(level, "ui.rpc_calls") or {}
    if not calls:
        return []
    ordered = sorted(calls.items(), key=lambda item: (-int(item[1].get("count") or 0), item[0]))
    rows = [
        [
            f"`{method}`",
            format_value(values.get("count")),
            format_value(values.get("total_ms")),
            format_value(values.get("max_ms")),
            format_value(values.get("failed")),
        ]
        for method, values in ordered[:TOP_REPORTED_RPC_METHODS]
    ]
    lines = ["", f"UI RPC calls by method (top {TOP_REPORTED_RPC_METHODS}):", ""]
    lines.extend(_markdown_table(["Method", "Calls", "Total ms", "Max ms", "Failed"], rows))
    return lines


def _counter_details(level: Mapping[str, Any]) -> list[str]:
    counters = get_path(level, "server.counters") or {}
    if not counters:
        return []
    ordered = sorted(counters.items(), key=lambda item: (-float(item[1]), item[0]))
    lines = ["", f"Server counters in the recording (top {TOP_REPORTED_COUNTERS}):", ""]
    rows = [[f"`{name}`", format_value(value)] for name, value in ordered[:TOP_REPORTED_COUNTERS]]
    lines.extend(_markdown_table(["Counter", "Total"], rows))
    return lines


_TIMELINE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("t s", "t_s"),
    ("RSS MB", "rss_mb"),
    ("Tasks", "asyncio_tasks"),
    ("Threads", "python_threads"),
    ("Runs", "runs_active"),
    ("gen2 n", "gc_gen2_count"),
    ("gen2 max ms", "gc_gen2_max_ms"),
    ("gen2 sum ms", "gc_gen2_sum_ms"),
    ("lag p99 ms", "lag_p99_ms"),
    ("Counters", "counters_total"),
)


def _timeline_details(level: Mapping[str, Any]) -> list[str]:
    samples = get_path(level, "timeline.samples") or []
    if not samples:
        return []
    interval = get_path(level, "timeline.interval_seconds")
    lines = [
        "",
        f"Server snapshots every {format_value(interval)} s (gen2 and lag figures are "
        "cumulative since the server started):",
        "",
    ]
    rows = [
        [format_value(sample.get(key)) for _label, key in _TIMELINE_COLUMNS]
        for sample in samples
        if "error" not in sample
    ]
    lines.extend(_markdown_table([label for label, _key in _TIMELINE_COLUMNS], rows))
    failed = [sample for sample in samples if "error" in sample]
    if failed:
        lines.append("")
        lines.extend(
            f"- Snapshot at {format_value(sample.get('t_s'))} s failed: {sample['error']}"
            for sample in failed
        )
    return lines


def _heap_details(level: Mapping[str, Any]) -> list[str]:
    heap = level.get("heap")
    if not isinstance(heap, Mapping):
        return []
    lines = [
        "",
        f"Heap census: {format_value(heap.get('tracked_start'))} -> "
        f"{format_value(heap.get('tracked_end'))} tracked objects "
        f"({format_value(heap.get('tracked_change'))}; "
        f"{format_value(heap.get('frozen_end'))} frozen startup objects not included).",
    ]
    for title, key in (("Types", "growth"), ("Modules", "module_growth")):
        rows = [
            [
                f"`{row.get('name')}`",
                format_value(row.get("count")),
                format_value(row.get("change")),
            ]
            for row in heap.get(key) or []
        ]
        if rows:
            lines.extend(["", f"{title} that grew most:", ""])
            lines.extend(_markdown_table(["Name", "Objects", "Change"], rows))
    return lines


def compare_results(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """Per level and metric: old, new, absolute and relative change."""
    old_levels = {level.get("agents"): level for level in old.get("levels") or []}
    new_levels = {level.get("agents"): level for level in new.get("levels") or []}
    shared = sorted(key for key in old_levels.keys() & new_levels.keys() if isinstance(key, int))
    lines = [
        f"Comparing {old.get('started_at')} ({get_path(old, 'git.commit')}) -> "
        f"{new.get('started_at')} ({get_path(new, 'git.commit')})"
    ]
    if scenario_of(old) != scenario_of(new):
        lines.append(
            f"Note: comparing a {scenario_of(old)} run with a {scenario_of(new)} run; "
            "the same metric can mean different work."
        )
    if not shared:
        lines.append("No concurrency level appears in both results.")
        return "\n".join(lines)
    for agents in shared:
        rows = [["Metric", "old", "new", "delta", "change"]]
        for key in COMPARE_KEYS:
            before = get_path(old_levels[agents], key)
            after = get_path(new_levels[agents], key)
            if not isinstance(before, int | float) or not isinstance(after, int | float):
                continue
            delta = after - before
            percent = f"{delta / before * 100:+.1f}%" if before else "-"
            worse = delta < 0 if key in HIGHER_IS_BETTER else delta > 0
            flag = " (worse)" if worse and delta != 0 else ""
            rows.append(
                [
                    key,
                    format_value(before),
                    format_value(after),
                    f"{'+' if delta > 0 else ''}{format_value(delta)}",
                    percent + flag,
                ]
            )
        widths = [max(len(row[index]) for row in rows) for index in range(5)]
        lines.extend(["", f"== {agents} agent(s) =="])
        for row in rows:
            lines.append(
                "  ".join(
                    cell.ljust(widths[index]) if index == 0 else cell.rjust(widths[index])
                    for index, cell in enumerate(row)
                )
            )
    return "\n".join(lines)


def load_result(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("kind") != RESULT_KIND:
        raise ValueError(f"{path} is not a perf_load result.json")
    return data


def write_result(run_dir: Path, result: Mapping[str, Any]) -> None:
    """Write ``result.json`` and ``report.md`` (atomically replaced per call)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("result.json", json.dumps(result, indent=2, sort_keys=False) + "\n"),
        ("report.md", render_markdown(result)),
    ):
        temporary = run_dir / f".{name}.tmp"
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(run_dir / name)
