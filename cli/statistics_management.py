"""Statistics report RPC commands for the vBot CLI.

Every subcommand issues one ``statistics.report`` call that requests only the
report section it renders, and formats that section as deterministic,
agent-facing plain text. ``compactions`` reads ``diagnostics.compactions`` and
``errors`` reads ``runs.errors``. The report shape is the section contract
documented in ``.vorch/domain-maps/statistics.md``; the CLI reads its
JSON-native fields and never re-derives anything server-side.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

# Ranked breakdowns and Run lists are bounded so one command never floods the
# terminal; the report itself may carry more rows.
_TOP_ROWS = 20
_TOP_RUNS = 10

_MISSING = "-"

Section = Mapping[str, Any]
Formatter = Callable[[Section, object], str]


def statistics_report(
    instance: ServerInstance,
    section: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> CommandResult:
    """Request the report section behind one CLI ``section`` and format it.

    ``since``/``until`` are passed through verbatim as raw ISO-8601 strings; the
    server owns their validation and the hour alignment it echoes back.
    """

    route = _SECTIONS.get(section)
    if route is None:
        return CommandResult(
            ok=False, message=f"unsupported statistics section: {section}", instance=instance
        )
    path, formatter = route
    payload = _rpc_call(instance, "statistics.report", _report_params(path[0], since, until))
    if not payload.ok:
        return payload.to_command_result()

    section_data: object = payload.data
    for key in path:
        section_data = section_data.get(key) if isinstance(section_data, dict) else None
    if not isinstance(section_data, dict):
        return CommandResult(
            ok=False,
            message=f"RPC result missing '{'.'.join(path)}' section",
            instance=instance,
        )
    return CommandResult(
        ok=True,
        message=formatter(section_data, payload.data.get("window")),
        instance=instance,
    )


def _report_params(report_section: str, since: str | None, until: str | None) -> dict[str, Any]:
    """Request one report section, with a window bound only when it was given."""

    params: dict[str, Any] = {"sections": [report_section]}
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    return params


# ---------------------------------------------------------------------------
# Section formatters - each renders one report section into plain text
# ---------------------------------------------------------------------------


def _format_overview(section: Section, window: object) -> str:
    lines = ["overview:", *_window_lines(window)]
    totals = _mapping(section.get("totals"))
    lines.extend(_totals_lines(totals))
    lines.append(f"runs: {_status_counts(section.get('runs'))}")
    lines.append(f"user runs: {_user_runs(section.get('user_runs'))}")
    lines.append(
        f"active: agents={_int(section.get('active_agents'))} "
        f"sessions={_int(section.get('active_sessions'))}"
    )
    previous = section.get("previous")
    if isinstance(previous, dict):
        previous_runs = _mapping(section.get("previous_runs"))
        lines.append(
            "previous window of equal length: "
            f"cost={_usd(previous.get('cost_usd'))} "
            f"calls={_int(previous.get('calls'))} "
            f"runs={_int(previous_runs.get('total'))}"
        )
    else:
        lines.append("previous window: none (all-time report; pass --since to compare)")

    lines.append("")
    lines.append("by origin:")
    lines.extend(
        _rows(
            section.get("by_origin"),
            lambda row: (
                f"  {_text(row.get('origin'))}: runs={_int(row.get('runs'))} "
                f"calls={_int(row.get('calls'))} {_token_pair(row)} "
                f"cost={_usd(row.get('cost_usd'))}"
            ),
            "  no activity recorded",
        )
    )

    lines.append("")
    lines.append("top agents by cost:")
    lines.extend(
        _rows(
            section.get("top_agents"),
            lambda row: (
                f"  {_text(row.get('agent_id'))}: runs={_int(row.get('runs'))} "
                f"calls={_int(row.get('calls'))} {_token_pair(row)} "
                f"cost={_usd(row.get('cost_usd'))}"
            ),
            "  no agent activity recorded",
        )
    )

    lines.append("")
    lines.append("top models by cost:")
    lines.extend(
        _rows(
            section.get("top_models"),
            lambda row: (
                f"  {_text(row.get('model'))}: calls={_int(row.get('calls'))} "
                f"{_token_pair(row)} cache_hit={_cache_hit(row)} "
                f"cost={_usd(row.get('cost_usd'))}"
            ),
            "  no model calls recorded",
        )
    )

    lines.append("")
    lines.append("insights:")
    lines.extend(_rows(section.get("insights"), _insight_line, "  none"))
    return "\n".join(lines)


def _user_runs(value: object) -> str:
    runs = _mapping(value)
    return (
        f"count={_int(runs.get('count'))} "
        f"duration_p50={_duration(runs.get('duration_p50_ms'))} "
        f"duration_p90={_duration(runs.get('duration_p90_ms'))} "
        f"cost_p50={_usd(runs.get('cost_p50_usd'))} "
        f"cost_p90={_usd(runs.get('cost_p90_usd'))} "
        f"first_visible_p50={_duration(runs.get('first_visible_p50_ms'))}"
    )


def _insight_line(row: Mapping[str, Any]) -> str:
    insight = _text(row.get("id"))
    values = _mapping(row.get("values"))
    describe = _INSIGHTS.get(insight)
    if describe is None:
        text = " ".join(f"{key}={values[key]}" for key in sorted(values))
    else:
        text = describe(values)
    return f"  [{_text(row.get('severity'))}] {insight}: {text}"


_INSIGHTS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "uncached_value": lambda values: (
        "estimated cost of calls that reported no cache counters is "
        f"{_usd(values.get('cost_usd'))}, {_percent(values.get('share'))} of estimated cost; "
        "these estimates assume no prompt caching "
        f"(top model {_text(values.get('top_model'))})"
    ),
    "cancelled_cost": lambda values: (
        f"cancelled Runs ({_int(values.get('runs'))}) cost {_usd(values.get('cost_usd'))}, "
        f"{_percent(values.get('share'))} of cost"
    ),
    "top_runs_share": lambda values: (
        f"the costliest 10% of Runs ({_int(values.get('runs'))}) hold "
        f"{_percent(values.get('share'))} of Run cost"
    ),
    "failed_attempt_burst": lambda values: (
        f"{_int(values.get('failed'))} failed Model requests in the hour from "
        f"{_text(values.get('hour_start'))}, most for {_text(values.get('model'))}"
    ),
    "runaway_runs": lambda values: f"Runs with 100 or more iterations: {_int(values.get('runs'))}",
    "tool_failure": lambda values: (
        f"Tool {_text(values.get('tool'))} rejected {_percent(values.get('rate'))} "
        f"of {_int(values.get('calls'))} calls"
    ),
}


def _format_usage(section: Section, window: object) -> str:
    lines = ["usage:", *_window_lines(window)]
    lines.extend(_totals_lines(_mapping(section.get("totals"))))
    breakdowns = _mapping(section.get("breakdowns"))
    for dimension, title in (
        ("model", "by model"),
        ("provider", "by provider"),
        ("agent", "by agent"),
        ("project", "by project"),
        ("origin", "by origin"),
        ("kind", "by request kind"),
    ):
        lines.append("")
        lines.append(f"{title}:")
        rows = [_breakdown_line(row, dimension) for row in _dict_rows(breakdowns.get(dimension))]
        lines.extend(rows[:_TOP_ROWS] or ["  no model calls recorded"])

    lines.append("")
    lines.append("costliest runs:")
    lines.extend(_run_lines(section.get("top_runs")))

    lines.append("")
    lines.append("costliest sessions:")
    lines.extend(
        _rows(
            section.get("top_sessions"),
            lambda row: (
                f"  {_text(row.get('agent_id'))} {_text(row.get('session_id'))}: "
                f"runs={_int(row.get('runs'))} calls={_int(row.get('calls'))} "
                f"{_token_pair(row)} cost={_usd(row.get('cost_usd'))}"
                f"{_title(row.get('session_title'))}"
            ),
            "  no sessions recorded",
            limit=_TOP_RUNS,
        )
    )
    return "\n".join(lines)


def _breakdown_line(row: Mapping[str, Any], dimension: str) -> str:
    key = row.get("key")
    label = "(no project)" if dimension == "project" and key == "" else _text(key)
    return (
        f"  {label}: calls={_int(row.get('calls'))} failed={_int(row.get('failed_calls'))} "
        f"runs={_int(row.get('runs'))} sessions={_int(row.get('sessions'))} "
        f"{_token_pair(row)} cache_hit={_cache_hit(row)} cost={_usd(row.get('cost_usd'))}"
    )


def _totals_lines(totals: Mapping[str, Any]) -> list[str]:
    """The shared ``Totals`` figures: cost and its coverage, calls, tokens, cache."""

    return [
        f"cost: {_usd(totals.get('cost_usd'))} "
        f"(reported={_usd(totals.get('reported_cost_usd'))} "
        f"over {_int(totals.get('reported_calls'))} calls, "
        f"estimated={_usd(totals.get('estimated_cost_usd'))} "
        f"over {_int(totals.get('estimated_calls'))} calls, "
        f"unpriced calls={_int(totals.get('unpriced_calls'))})",
        f"model calls: {_int(totals.get('calls'))} "
        f"(failed={_int(totals.get('failed_calls'))}, "
        f"without usage={_int(totals.get('unreported_calls'))})",
        f"tokens: {_token_pair(totals)} reasoning={_int(totals.get('reasoning_tokens'))} "
        f"(of which estimated: input={_int(totals.get('estimated_input_tokens'))} "
        f"output={_int(totals.get('estimated_output_tokens'))})",
        f"cache: hit_rate={_cache_hit(totals)} "
        f"(read={_int(totals.get('cache_read_tokens'))} "
        f"of input={_int(totals.get('cache_input_tokens'))} "
        f"over {_int(totals.get('cache_calls'))} cache-reporting calls) "
        f"write={_int(totals.get('cache_write_tokens'))}",
    ]


def _format_runs(section: Section, window: object) -> str:
    lines = ["runs:", *_window_lines(window)]
    lines.append(f"runs: {_status_counts(section.get('totals'))}")
    previous = section.get("previous")
    if isinstance(previous, dict):
        previous_user = _mapping(previous.get("user"))
        lines.append(
            "previous window of equal length: "
            f"{_status_counts(previous.get('totals'))} "
            f"user_duration_p50={_duration(previous_user.get('duration_p50_ms'))} "
            f"user_duration_p90={_duration(previous_user.get('duration_p90_ms'))}"
        )
    else:
        lines.append("previous window: none (all-time report; pass --since to compare)")
    cancelled = _mapping(section.get("cancelled"))
    lines.append(
        f"cancelled: runs={_int(cancelled.get('runs'))} "
        f"cost={_usd(cancelled.get('cost_usd'))} "
        f"wait_p50={_duration(cancelled.get('wait_p50_ms'))}"
    )
    errors = _mapping(section.get("errors"))
    lines.append(
        f"errors: {_int(errors.get('total'))} "
        f"(failed model requests={_int(errors.get('failed_attempts'))}; "
        "details: vbot statistics errors)"
    )
    lines.append(
        "run cost: the Model requests made inside each Run, counted in the window the Run "
        "started in; requests outside any Run (background work) count only in overview "
        "and usage"
    )

    lines.append("")
    lines.append("by origin:")
    lines.extend(
        _rows(
            section.get("by_origin"),
            lambda row: f"  {_text(row.get('origin'))}: {_run_group(row)}",
            "  no runs recorded",
        )
    )

    lines.append("")
    lines.append("by agent:")
    lines.extend(
        _rows(
            section.get("agents"),
            lambda row: (
                f"  {_text(row.get('agent_id'))}: {_run_group(row)} "
                f"tool_time={_duration(row.get('tool_ms'))} "
                f"changed_files={_int(row.get('changed_files'))} "
                f"lines=+{_int(row.get('lines_added'))}/-{_int(row.get('lines_removed'))}"
            ),
            "  no runs recorded",
        )
    )

    for key, title in (
        ("longest", "longest runs"),
        ("costliest", "costliest runs"),
        ("most_steps", "runs with most model steps"),
    ):
        lines.append("")
        lines.append(f"{title}:")
        lines.extend(_run_lines(section.get(key)))
    return "\n".join(lines)


def _run_group(row: Mapping[str, Any]) -> str:
    """Status counts, duration and cost percentiles and averages of a Run group."""

    return (
        f"runs={_int(row.get('runs'))} completed={_int(row.get('completed'))} "
        f"failed={_int(row.get('failed'))} cancelled={_int(row.get('cancelled'))} "
        f"interrupted={_int(row.get('interrupted'))} "
        f"duration_p50={_duration(row.get('duration_p50_ms'))} "
        f"duration_p90={_duration(row.get('duration_p90_ms'))} "
        f"cost={_usd(row.get('cost_usd'))} cost_p50={_usd(row.get('cost_p50_usd'))} "
        f"{_cost_p90(row)}avg_tool_calls={_number(row.get('avg_tool_calls'))} "
        f"avg_model_steps={_number(row.get('avg_model_steps'))}"
    )


def _cost_p90(row: Mapping[str, Any]) -> str:
    if "cost_p90_usd" not in row:
        return ""
    return f"cost_p90={_usd(row.get('cost_p90_usd'))} "


def _run_lines(runs: object) -> list[str]:
    return _rows(
        runs,
        lambda run: (
            f"  {_text(run.get('agent_id'))} {_text(run.get('session_id'))} "
            f"{_text(run.get('run_id'))}: origin={_text(run.get('origin'))} "
            f"status={_text(run.get('status'))} started={_text(run.get('started_at'))} "
            f"duration={_duration(run.get('duration_ms'))} cost={_usd(run.get('cost_usd'))} "
            f"model_steps={_int(run.get('model_steps'))} "
            f"tool_calls={_int(run.get('tool_calls'))} "
            f"model={_text(run.get('primary_model'), missing=_MISSING)}"
        ),
        "  no runs recorded",
        limit=_TOP_RUNS,
    )


def _format_compactions(section: Section, window: object) -> str:
    lines = ["compactions:", *_window_lines(window)]
    lines.append(f"total compactions: {_int(section.get('total_compactions'))}")
    lines.append(f"sessions with compactions: {_int(section.get('sessions_with_compactions'))}")
    lines.append(
        "compactions per compacted session: "
        f"average={_number(section.get('average_per_compacted_session'))} "
        f"p50={_number(section.get('p50_per_compacted_session'))} "
        f"p95={_number(section.get('p95_per_compacted_session'))} "
        f"max={_int(section.get('max_per_session'))}"
    )

    reclaim = section.get("reclaim")
    if isinstance(reclaim, dict):
        lines.append(
            "estimated reclaimed tokens: "
            f"observations={_int(reclaim.get('observations'))} "
            f"total={_int(reclaim.get('total_tokens'))} "
            f"average={_number(reclaim.get('average_tokens'))} "
            f"p50={_number(reclaim.get('p50_tokens'))} "
            f"p95={_number(reclaim.get('p95_tokens'))}"
        )

    lines.append("")
    lines.append("by strategy:")
    lines.extend(
        _rows(
            section.get("by_strategy"),
            lambda row: f"  {_text(row.get('strategy'))}: {_int(row.get('compactions'))}",
            "  no compactions recorded",
        )
    )

    lines.append("")
    lines.append("most compacted sessions:")
    lines.extend(
        _rows(
            section.get("top_sessions"),
            lambda row: (
                f"  {_text(row.get('agent_id'))} {_text(row.get('session_id'))}: "
                f"compactions={_int(row.get('compactions'))} "
                f"estimated_reclaimed_tokens={_int(row.get('estimated_reclaimed_tokens'))} "
                f"last_compaction={_text(row.get('last_compaction'), missing=_MISSING)}"
            ),
            "  no compacted sessions",
        )
    )
    return "\n".join(lines)


def _format_errors(section: Section, window: object) -> str:
    lines = ["errors:", *_window_lines(window)]
    lines.append(f"total errors: {_int(section.get('total'))}")
    lines.append(f"failed model requests: {_int(section.get('failed_attempts'))}")
    lines.append(
        "attribution: persisted Run errors; provider/model use the last preceding "
        "Assistant Model step (proxy); failed model requests are failed attempts in "
        "the usage ledger, retried ones included; Tool rejections are separate "
        "(vbot statistics tools)"
    )
    for key, title in (
        ("by_kind", "by kind"),
        ("by_provider", "by provider"),
        ("by_model", "by model"),
        ("by_agent", "by agent"),
    ):
        lines.append("")
        lines.append(f"{title}:")
        lines.extend(_count_lines(section.get(key)))

    lines.append("")
    zone = _mapping(window).get("timezone")
    lines.append(f"by local hour ({_text(zone)}):")
    lines.extend(
        _rows(
            [row for row in _dict_rows(section.get("by_hour")) if _int(row.get("count"))],
            lambda row: f"  {_int(row.get('hour')):02d}: {_int(row.get('count'))}",
            "  no errors recorded",
            limit=24,
        )
    )
    return "\n".join(lines)


def _count_lines(entries: object) -> list[str]:
    return _rows(
        entries,
        lambda row: f"  {_text(row.get('key'))}: {_int(row.get('count'))}",
        "  no errors recorded",
    )


def _format_tools(section: Section, window: object) -> str:
    lines = ["tools:", *_window_lines(window)]
    totals = _mapping(section.get("totals"))
    lines.append(
        f"calls: {_int(totals.get('calls'))} (accepted={_int(totals.get('accepted'))} "
        f"rejected={_int(totals.get('rejected'))} unknown={_int(totals.get('unknown'))}) "
        f"tools={_int(totals.get('tools'))} tool_time={_duration(totals.get('tool_ms'))}"
    )
    lines.append("latency: p50/p95 are approximate (from duration histograms)")
    lines.append("arguments: not read or included by Statistics")

    lines.append("")
    lines.append("tools:")
    lines.extend(
        _rows(
            section.get("tools"),
            lambda row: (
                f"  {_text(row.get('name'))}: calls={_int(row.get('calls'))} "
                f"rejected={_int(row.get('rejected'))} "
                f"({_percent(row.get('rejection_rate'))}) "
                f"p50={_duration(row.get('p50_ms'))} p95={_duration(row.get('p95_ms'))} "
                f"max={_duration(row.get('max_ms'))} total={_duration(row.get('total_ms'))} "
                f"time_share={_percent(row.get('time_share'))} "
                f"top_rejections={_codes(row.get('top_codes'))}"
            ),
            "  no tool calls recorded",
        )
    )

    lines.append("")
    lines.append("rejection codes:")
    lines.extend(
        _rows(
            section.get("rejection_codes"),
            lambda row: (
                f"  {_text(row.get('code'))}: {_int(row.get('count'))} "
                f"(tools: {_join(row.get('tools'))})"
            ),
            "  no rejections recorded",
        )
    )

    lines.append("")
    lines.append("by agent:")
    lines.extend(
        _rows(
            section.get("by_agent"),
            lambda row: (
                f"  {_text(row.get('agent_id'))}: calls={_int(row.get('calls'))} "
                f"rejected={_int(row.get('rejected'))} "
                f"tool_time={_duration(row.get('tool_ms'))}"
            ),
            "  no tool calls recorded",
        )
    )
    return "\n".join(lines)


def _codes(entries: object) -> str:
    rows = _dict_rows(entries)
    if not rows:
        return _MISSING
    return ",".join(f"{_text(row.get('code'))}:{_int(row.get('count'))}" for row in rows)


def _format_skills(section: Section, window: object) -> str:
    """Render evidence-backed Skill candidates separately from missing offer data."""

    lines = ["skills:", *_window_lines(window)]
    lines.append(f"total skills: {_int(section.get('total_skills'))}")
    lines.append(f"activated skills: {_int(section.get('used_skills'))}")
    lines.append(f"without offer conversion: {_int(section.get('offered_unactivated_skills'))}")
    lines.append(f"without offer data: {_int(section.get('skills_without_offer_data'))}")

    rows = _dict_rows(section.get("skills"))

    lines.append("")
    lines.append("without offer conversion:")
    lines.extend(_offered_unactivated_lines(rows))

    lines.append("")
    lines.append("without offer data:")
    lines.extend(_without_offer_data_lines(rows))

    lines.append("")
    lines.append("per skill:")
    lines.extend(_skill_usage_lines(rows))
    return "\n".join(lines)


def _offered_unactivated_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    candidates = [
        row
        for row in rows
        if _int(row.get("offered_sessions")) > 0
        and _int(row.get("activated_offered_sessions")) == 0
    ]
    if not candidates:
        return ["  no evidence-backed candidates"]
    return [f"  {_text(row.get('name'))} [{_join(row.get('origins'))}]" for row in candidates]


def _without_offer_data_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    missing = [row for row in rows if _int(row.get("offered_sessions")) == 0]
    if not missing:
        return ["  all skills have offer data"]
    return [f"  {_text(row.get('name'))} [{_join(row.get('origins'))}]" for row in missing]


def _skill_usage_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Per-Skill usage rows with conversion limited to observed offers."""

    if not rows:
        return ["  no skills recorded"]
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"  {_text(row.get('name'))} [{_join(row.get('origins'))}]: "
            f"offered={_int(row.get('offered_sessions'))} "
            f"activated={_int(row.get('activated_sessions'))} "
            f"activated_after_offer={_int(row.get('activated_offered_sessions'))} "
            f"offer_conversion={_number(row.get('usage_rate'))} "
            f"last_activated={_text(row.get('last_activated'), missing=_MISSING)}"
        )
    return lines


# ---------------------------------------------------------------------------
# Shared value/line helpers
# ---------------------------------------------------------------------------


def _window_lines(window: object) -> list[str]:
    """Echo the applied time window so ranked output is never read out of context."""

    if not isinstance(window, dict):
        return []
    since = window.get("since")
    until = window.get("until")
    if since is None and until is None:
        return ["window: all time"]
    return [
        f"window: since={_text(since, missing=_MISSING)} until={_text(until, missing=_MISSING)}"
    ]


def _rows(
    entries: object,
    render: Callable[[Mapping[str, Any]], str],
    empty: str,
    *,
    limit: int = _TOP_ROWS,
) -> list[str]:
    rows = _dict_rows(entries)
    if not rows:
        return [empty]
    return [render(row) for row in rows[:limit]]


def _status_counts(value: object) -> str:
    counts = _mapping(value)
    return " ".join(
        f"{status}={_int(counts.get(status))}"
        for status in ("total", "completed", "failed", "cancelled", "interrupted", "running")
    )


def _token_pair(row: Mapping[str, Any]) -> str:
    return f"input={_int(row.get('input_tokens'))} output={_int(row.get('output_tokens'))}"


def _cache_hit(row: Mapping[str, Any]) -> str:
    """Cache read share of the input of cache-reporting calls; ``-`` without any."""

    cache_input = _int(row.get("cache_input_tokens"))
    if not _int(row.get("cache_calls")) or not cache_input:
        return _MISSING
    return _percent(_int(row.get("cache_read_tokens")) / cache_input)


def _title(value: object) -> str:
    if isinstance(value, str) and value:
        return f' title="{value}"'
    return ""


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _MISSING
    return f"{value:.2f}"


def _percent(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _MISSING
    return f"{value * 100:.1f}%"


def _usd(value: object) -> str:
    """A USD amount; ``unknown`` when no request of the figure has a price."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"${value:.4f}"


def _duration(value: object) -> str:
    """Milliseconds as ``850ms``, ``12.3s``, ``4m05s`` or ``2h03m``; ``-`` when unknown."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _MISSING
    ms = round(value)
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    seconds = ms // 1000
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def _text(value: object, *, missing: str = "?") -> str:
    if isinstance(value, str) and value:
        return value
    return missing


def _join(values: object) -> str:
    if not isinstance(values, list):
        return _MISSING
    parts = [item for item in values if isinstance(item, str) and item]
    return ", ".join(parts) if parts else _MISSING


# CLI section -> (path of the formatted object in the report, formatter). The
# first path element is the report section the command requests.
_SECTIONS: dict[str, tuple[tuple[str, ...], Formatter]] = {
    "overview": (("overview",), _format_overview),
    "usage": (("usage",), _format_usage),
    "runs": (("runs",), _format_runs),
    "compactions": (("diagnostics", "compactions"), _format_compactions),
    "errors": (("runs", "errors"), _format_errors),
    "tools": (("tools",), _format_tools),
    "skills": (("skills",), _format_skills),
}
