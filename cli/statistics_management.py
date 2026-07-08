"""Statistics report RPC commands for the vBot CLI.

Every subcommand issues one ``statistics.report`` RPC call and formats only the
requested section of the returned report as deterministic, agent-facing plain
text. The report shape is the on-demand aggregation contract documented in
``.vorch/domain-maps/statistics.md``; the CLI reads its JSON-native fields and
never re-derives anything server-side.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance

# The report bounds its own output lists to a stable top-N (see
# core/statistics/statistics.py); the CLI mirrors that ceiling when rendering
# ranked breakdowns so a single command never floods the terminal.
_TOP_ROWS = 20

_MISSING_TIMESTAMP = "-"


def statistics_report(
    instance: ServerInstance,
    section: str,
    *,
    since: str | None = None,
    until: str | None = None,
) -> CommandResult:
    """Fetch the full report via `statistics.report` and format one section.

    ``since``/``until`` are passed through verbatim as raw ISO-8601 strings; the
    server owns their validation. Only the requested ``section`` is rendered so
    each subcommand stays focused and its output is deterministic.
    """

    params = _report_params(since, until)
    payload = _rpc_call(instance, "statistics.report", params)
    if not payload.ok:
        return payload.to_command_result()

    formatter = _SECTION_FORMATTERS.get(section)
    if formatter is None:
        return CommandResult(
            ok=False, message=f"unsupported statistics section: {section}", instance=instance
        )

    section_data = payload.data.get(section)
    if not isinstance(section_data, dict):
        return CommandResult(
            ok=False,
            message=f"RPC result missing '{section}' section",
            instance=instance,
        )
    window = payload.data.get("window")
    return CommandResult(
        ok=True,
        message=formatter(section_data, window),
        instance=instance,
    )


def _report_params(since: str | None, until: str | None) -> dict[str, Any]:
    """Build the RPC params, including a window bound only when it was given."""

    params: dict[str, Any] = {}
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    return params


# ---------------------------------------------------------------------------
# Section formatters — each renders one report section into plain text
# ---------------------------------------------------------------------------


def _format_overview(section: Mapping[str, Any], window: object) -> str:
    lines = ["overview:", *_window_lines(window)]
    lines.append(f"agents: {_int(section.get('total_agents'))}")
    lines.append(f"sessions: {_int(section.get('total_sessions'))}")
    lines.append(f"runs: {_int(section.get('total_runs'))}")
    lines.append(f"open run groups: {_int(section.get('open_run_groups'))}")
    lines.append(f"messages: {_int(section.get('total_messages'))}")
    lines.append(f"last activity: {_timestamp(section.get('last_activity'))}")

    status = section.get("run_status")
    if isinstance(status, dict):
        lines.append(
            "run status: "
            f"completed={_int(status.get('completed'))} "
            f"failed={_int(status.get('failed'))} "
            f"cancelled={_int(status.get('cancelled'))}"
        )
    lines.append(f"average run duration ms: {_number(section.get('average_run_duration_ms'))}")
    lines.append(f"median run duration ms: {_number(section.get('median_run_duration_ms'))}")
    lines.append(f"runs with tool calls: {_int(section.get('runs_with_tool_calls'))}")
    lines.append(f"total tool calls: {_int(section.get('total_tool_calls'))}")

    lines.append("")
    lines.append("messages by role:")
    lines.extend(_messages_by_role_lines(section.get("messages_by_role")))

    lines.append("")
    lines.append("agents:")
    lines.extend(_agent_activity_lines(section.get("agents")))
    return "\n".join(lines)


def _messages_by_role_lines(roles: object) -> list[str]:
    if not isinstance(roles, dict) or not roles:
        return ["  no messages recorded"]
    return [f"  {role}: {_int(count)}" for role, count in roles.items()]


def _agent_activity_lines(agents: object) -> list[str]:
    rows = _dict_rows(agents)
    if not rows:
        return ["  no agent activity recorded"]
    lines: list[str] = []
    for agent in rows:
        lines.append(
            f"  {_text(agent.get('agent_id'))}: "
            f"sessions={_int(agent.get('sessions'))} "
            f"runs={_int(agent.get('runs'))} "
            f"messages={_int(agent.get('messages'))} "
            f"errors={_int(agent.get('errors'))} "
            f"last_activity={_timestamp(agent.get('last_activity'))}"
        )
    return lines


def _format_usage(section: Mapping[str, Any], window: object) -> str:
    lines = ["usage:", *_window_lines(window)]

    totals = section.get("totals")
    if isinstance(totals, dict):
        lines.append(f"assistant messages: {_int(totals.get('assistant_messages'))}")
        lines.append(f"measured turns: {_int(totals.get('measured_turns'))}")
        lines.append(f"estimated turns: {_int(totals.get('estimated_turns'))}")
        lines.append(f"measured input tokens: {_int(totals.get('measured_input_tokens'))}")
        lines.append(f"measured output tokens: {_int(totals.get('measured_output_tokens'))}")
        lines.append(f"estimated input tokens: {_int(totals.get('estimated_input_tokens'))}")
        lines.append(f"estimated output tokens: {_int(totals.get('estimated_output_tokens'))}")
        lines.append(f"cache read tokens: {_int(totals.get('cache_read_tokens'))}")
        lines.append(f"cache write tokens: {_int(totals.get('cache_write_tokens'))}")

    lines.append("")
    lines.append("providers:")
    lines.extend(_provider_usage_lines(section.get("providers")))

    lines.append("")
    lines.append("models:")
    lines.extend(_model_usage_lines(section.get("models")))
    return "\n".join(lines)


def _provider_usage_lines(providers: object) -> list[str]:
    rows = _dict_rows(providers)
    if not rows:
        return ["  no provider usage recorded"]
    lines: list[str] = []
    for provider in rows[:_TOP_ROWS]:
        lines.append(
            f"  {_text(provider.get('provider'))}: "
            f"runs={_int(provider.get('runs'))} "
            f"assistant_messages={_int(provider.get('assistant_messages'))} "
            f"total_tokens={_int(provider.get('total_tokens'))} "
            f"errors={_int(provider.get('errors'))}"
        )
    return lines


def _model_usage_lines(models: object) -> list[str]:
    rows = _dict_rows(models)
    if not rows:
        return ["  no model usage recorded"]
    lines: list[str] = []
    for model in rows[:_TOP_ROWS]:
        lines.append(
            f"  {_text(model.get('model'))}: "
            f"runs={_int(model.get('runs'))} "
            f"assistant_messages={_int(model.get('assistant_messages'))} "
            f"total_tokens={_int(model.get('total_tokens'))} "
            f"errors={_int(model.get('errors'))}"
        )
    return lines


def _format_runs(section: Mapping[str, Any], window: object) -> str:
    lines = ["runs:", *_window_lines(window)]
    lines.append(f"total runs: {_int(section.get('total_runs'))}")
    lines.append(f"open run groups: {_int(section.get('open_run_groups'))}")

    status = section.get("status")
    if isinstance(status, dict):
        lines.append(
            "status: "
            f"completed={_int(status.get('completed'))} "
            f"failed={_int(status.get('failed'))} "
            f"cancelled={_int(status.get('cancelled'))}"
        )
    lines.append(f"cancel rate: {_number(section.get('cancel_rate'))}")
    lines.append(f"failure rate: {_number(section.get('failure_rate'))}")

    duration = section.get("duration")
    if isinstance(duration, dict):
        lines.append(
            "duration ms: "
            f"count={_int(duration.get('count'))} "
            f"average={_number(duration.get('average_ms'))} "
            f"p50={_number(duration.get('p50_ms'))} "
            f"p90={_number(duration.get('p90_ms'))} "
            f"p95={_number(duration.get('p95_ms'))}"
        )
    lines.append(f"runs with tool calls: {_int(section.get('runs_with_tool_calls'))}")
    lines.append(f"total tool calls: {_int(section.get('total_tool_calls'))}")
    lines.append(
        f"average tool calls per run: {_number(section.get('average_tool_calls_per_run'))}"
    )
    lines.append(f"derived fallback runs: {_int(section.get('derived_fallback_runs'))}")

    lines.append("")
    lines.append("runs per agent:")
    lines.extend(_agent_run_count_lines(section.get("runs_per_agent")))

    lines.append("")
    lines.append("longest runs:")
    lines.extend(_longest_run_lines(section.get("longest_runs")))
    return "\n".join(lines)


def _agent_run_count_lines(entries: object) -> list[str]:
    rows = _dict_rows(entries)
    if not rows:
        return ["  no runs recorded"]
    return [f"  {_text(row.get('agent_id'))}: {_int(row.get('runs'))}" for row in rows[:_TOP_ROWS]]


def _longest_run_lines(runs: object) -> list[str]:
    rows = _dict_rows(runs)
    if not rows:
        return ["  no runs recorded"]
    lines: list[str] = []
    for run in rows[:_TOP_ROWS]:
        lines.append(
            f"  {_text(run.get('agent_id'))} {_text(run.get('session_id'))}: "
            f"status={_text(run.get('status'))} "
            f"duration_ms={_int(run.get('duration_ms'))} "
            f"models={_join(run.get('models'))}"
        )
    return lines


def _format_errors(section: Mapping[str, Any], window: object) -> str:
    lines = ["errors:", *_window_lines(window)]
    lines.append(f"total errors: {_int(section.get('total_errors'))}")

    lines.append("")
    lines.append("by kind:")
    lines.extend(_count_entry_lines(section.get("by_kind")))

    lines.append("")
    lines.append("by provider:")
    lines.extend(_count_entry_lines(section.get("by_provider")))

    lines.append("")
    lines.append("by model:")
    lines.extend(_count_entry_lines(section.get("by_model")))

    lines.append("")
    lines.append("by agent:")
    lines.extend(_count_entry_lines(section.get("by_agent")))
    return "\n".join(lines)


def _count_entry_lines(entries: object) -> list[str]:
    rows = _dict_rows(entries)
    if not rows:
        return ["  no errors recorded"]
    return [f"  {_text(row.get('key'))}: {_int(row.get('count'))}" for row in rows[:_TOP_ROWS]]


def _format_tools(section: Mapping[str, Any], window: object) -> str:
    lines = ["tools:", *_window_lines(window)]
    lines.append(f"total calls: {_int(section.get('total_calls'))}")

    lines.append("")
    lines.append("tools:")
    lines.extend(_tool_stat_lines(section.get("tools")))

    lines.append("")
    lines.append("by agent:")
    lines.extend(_tool_by_agent_lines(section.get("by_agent")))
    return "\n".join(lines)


def _tool_stat_lines(tools: object) -> list[str]:
    rows = _dict_rows(tools)
    if not rows:
        return ["  no tool calls recorded"]
    lines: list[str] = []
    for tool in rows[:_TOP_ROWS]:
        lines.append(
            f"  {_text(tool.get('name'))}: "
            f"calls={_int(tool.get('calls'))} "
            f"successes={_int(tool.get('successes'))} "
            f"failures={_int(tool.get('failures'))} "
            f"success_rate={_number(tool.get('success_rate'))} "
            f"top_error={_text(tool.get('top_error_code'))}"
        )
    return lines


def _tool_by_agent_lines(entries: object) -> list[str]:
    rows = _dict_rows(entries)
    if not rows:
        return ["  no tool calls recorded"]
    return [f"  {_text(row.get('key'))}: {_int(row.get('count'))}" for row in rows[:_TOP_ROWS]]


def _format_skills(section: Mapping[str, Any], window: object) -> str:
    """Render the skills section: never-used list first, then the usage table.

    The never-used list leads because it is the agent's primary decision input
    for deleting or improving unused skills; the per-skill table follows with
    the usage detail behind each name.
    """

    lines = ["skills:", *_window_lines(window)]
    lines.append(f"total skills: {_int(section.get('total_skills'))}")
    lines.append(f"used skills: {_int(section.get('used_skills'))}")
    lines.append(f"never used skills: {_int(section.get('never_used_skills'))}")

    rows = _dict_rows(section.get("skills"))

    lines.append("")
    lines.append("never used:")
    lines.extend(_never_used_lines(rows))

    lines.append("")
    lines.append("per skill:")
    lines.extend(_skill_usage_lines(rows))
    return "\n".join(lines)


def _never_used_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """List inventory skills with zero lifetime activations, origins included."""

    never_used = [row for row in rows if _int(row.get("activated_sessions")) == 0]
    if not never_used:
        return ["  no unused skills"]
    return [f"  {_text(row.get('name'))} [{_join(row.get('origins'))}]" for row in never_used]


def _skill_usage_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Per-skill usage rows: name, origins, offered, activated, rate, last used."""

    if not rows:
        return ["  no skills recorded"]
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"  {_text(row.get('name'))} [{_join(row.get('origins'))}]: "
            f"offered={_int(row.get('offered_sessions'))} "
            f"activated={_int(row.get('activated_sessions'))} "
            f"usage_rate={_usage_rate(row.get('usage_rate'))} "
            f"last_activated={_timestamp(row.get('last_activated'))}"
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
    return [f"window: since={_timestamp(since)} until={_timestamp(until)}"]


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
        return _MISSING_TIMESTAMP
    return f"{value:.2f}"


def _usage_rate(value: object) -> str:
    """Format the offered/activated ratio; ``-`` when the skill was never offered."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _MISSING_TIMESTAMP
    return f"{value:.2f}"


def _timestamp(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    return _MISSING_TIMESTAMP


def _text(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    return "?"


def _join(values: object) -> str:
    if not isinstance(values, list):
        return _MISSING_TIMESTAMP
    parts = [item for item in values if isinstance(item, str) and item]
    return ", ".join(parts) if parts else _MISSING_TIMESTAMP


_SECTION_FORMATTERS = {
    "overview": _format_overview,
    "usage": _format_usage,
    "runs": _format_runs,
    "errors": _format_errors,
    "tools": _format_tools,
    "skills": _format_skills,
}
