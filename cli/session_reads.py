"""RPC-backed Session transcript, summary and consumption output."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from cli._pagination import encode_cursor
from cli._recovery import format_command, target_options
from cli.formatting import inline_text
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance
from cli.session_management import SESSION_TEXT_BUDGET, _session_list_failure

type JsonObject = dict[str, Any]


def session_show(
    instance: ServerInstance,
    session_id: str,
    *,
    agent_id: str | None = None,
    summary: bool = False,
    limit: int = 20,
    cursor: dict[str, object] | None = None,
    run_id: str | None = None,
    entry: int | None = None,
    offset: int = 0,
    json_output: bool = False,
) -> CommandResult:
    params: JsonObject = {"session_id": session_id, "summary": summary, "limit": limit}
    for key, value in (
        ("agent_id", agent_id),
        ("cursor", cursor),
        ("run_id", run_id),
        ("entry", entry),
    ):
        if value is not None:
            params[key] = value
    if offset:
        params["offset"] = offset
    while True:
        payload = _rpc_call(instance, "session.show", params)
        if not payload.ok:
            return _session_list_failure(payload.to_command_result(), json_output)
        data = dict(payload.data)
        if not isinstance(data.get("session"), dict) or not isinstance(data.get("entries"), list):
            return _failure(
                instance, "RPC result missing Session or transcript entries", json_output
            )
        if json_output:
            if isinstance(data.get("next_cursor"), dict):
                data["next_cursor"] = encode_cursor(data["next_cursor"])
            return _success(instance, json.dumps({"ok": True, **data}, ensure_ascii=True))
        message = _show_text(instance, data, params)
        if len(message) + 1 <= SESSION_TEXT_BUDGET:
            return _success(instance, message)
        if params["limit"] <= 1 or entry is not None:
            return _failure(
                instance,
                "This record exceeds the text budget; use --json for structured data",
                False,
            )
        # Repeat the same page with a smaller limit; never skip unseen entries.
        params = {**params, "limit": max(1, params["limit"] // 2)}


def session_usage(
    instance: ServerInstance,
    session_ids: Sequence[str],
    *,
    agent_id: str | None = None,
    json_output: bool = False,
) -> CommandResult:
    params: JsonObject = {"session_ids": list(session_ids)}
    if agent_id is not None:
        params["agent_id"] = agent_id
    payload = _rpc_call(instance, "statistics.session_usage", params)
    if not payload.ok:
        return _session_list_failure(payload.to_command_result(), json_output)
    data = dict(payload.data)
    rows = data.get("sessions")
    if not isinstance(rows, list) or not isinstance(data.get("total"), dict):
        return _failure(instance, "RPC result missing Session usage totals", json_output)
    if json_output:
        return _success(instance, json.dumps({"ok": True, **data}, ensure_ascii=True))
    lines = [
        f"Usage for {len(rows)} selected Sessions; "
        f"{data.get('session_count', '?')} distinct Sessions including Sub-Agents"
    ]
    lines.append(
        "Note: own Runs only; inherited history is excluded. "
        "Costs are USD; estimates are API equivalents, not subscription invoices."
    )
    for row in rows:
        lines.append(f"Session {_text(row.get('session_id'))} ({_text(row.get('agent_address'))}):")
        lines.append("  own: " + _usage_line(row.get("session", {})))
        lines.append(
            f"  Sub-Agents ({row.get('subagent_count', 0)}): "
            + _usage_line(row.get("subagents", {}))
        )
        lines.append("  tree total: " + _usage_line(row.get("total", {})))
    lines.append("Combined total (each Session counted once): " + _usage_line(data["total"]))
    message = "\n".join(lines)
    if len(message) + 1 > SESSION_TEXT_BUDGET:
        return _failure(
            instance, "Usage exceeds the text budget; select fewer ids or use --json", False
        )
    return _success(instance, message)


def _show_text(instance: ServerInstance, data: JsonObject, params: JsonObject) -> str:
    session = data["session"]
    session_id = str(session["id"])
    owner = str(session["agent_address"])
    base = ("vbot", "session", "show", session_id, "--agent", owner, *target_options(instance))
    title = _text(session.get("title") or session.get("auto_title"), 160)
    mode = "summary" if data.get("summary") else "transcript"
    lines = [
        f"Session {_text(session_id)} {mode}: {title}",
        f"Agent: {_text(owner)}; activity: {_text(session.get('activity_state'))}",
    ]
    waiting = session.get("waiting_for")
    if isinstance(waiting, dict) and any(waiting.values()):
        lines.append(
            "Waiting for: " + ", ".join(f"{key}={value}" for key, value in waiting.items() if value)
        )
    if params.get("run_id"):
        lines.append(f"Run: {_text(params['run_id'])}")
    if data.get("summary"):
        for key, label in (("task", "Initial task"), ("latest_answer", "Latest Assistant text")):
            item = data.get(key)
            if isinstance(item, dict):
                lines.append(
                    f"{label} [entry {item['seq']}]: {_text(item.get('text'))}"
                    + (" ... [shortened]" if item.get("truncated") else "")
                )
    entries = data["entries"]
    lines.append(
        f"Entries: {len(entries)}, chronological within this page;"
        + (" older entries available below" if data.get("next_cursor") else " no older entries")
    )
    shortened = False
    last_run = None
    context_sequences = {
        data[key]["seq"] for key in ("task", "latest_answer") if isinstance(data.get(key), dict)
    }
    for item in entries:
        if data.get("summary"):
            if item.get("run_id") and item["run_id"] != last_run:
                lines.append(f"Run: {_text(item['run_id'])}")
                last_run = item["run_id"]
            heading = f"  [{item['seq']}] {_text(item.get('role'))}"
        else:
            heading = f"  [{item['seq']}] {_text(item.get('at'))} {_text(item.get('role'))}"
            if item.get("run_id"):
                heading += f" run={_text(item['run_id'])}"
        if item.get("tool"):
            heading += f" {_text(item['tool'])}: {_text(item.get('action'))}"
            if item.get("action_truncated"):
                heading += " ... [shortened]"
        if item.get("status"):
            heading += f" [{_text(item['status'])}]"
        if item.get("result_status"):
            heading += f" recorded-result={_text(item['result_status'])}"
        if item.get("command_id"):
            heading += f" command={_text(item['command_id'])}"
        if item.get("exit_code") is not None:
            heading += f" exit={_text(item['exit_code'])}"
        lines.append(heading)
        if item.get("error"):
            lines.append("    Error: " + _text(item["error"]))
        for change in item.get("file_changes", []):
            lines.append(
                f"    {_text(change.get('change'))}: {_text(change.get('path'))} "
                + ("... [shortened] " if change.get("path_truncated") else "")
                + f"+{change.get('added', '?')}/-{change.get('removed', '?')}"
            )
        if item.get("more_file_changes"):
            lines.append("    Note: more file changes exist; inspect the complete Tool Result.")
        if item.get("arguments"):
            lines.append(
                "    Arguments: "
                + _text(item["arguments"])
                + (" ... [shortened]" if item.get("arguments_truncated") else "")
            )
        if item.get("text") and not (data.get("summary") and item["seq"] in context_sequences):
            lines.append(
                "    " + _text(item["text"]) + (" ... [shortened]" if item.get("truncated") else "")
            )
        for call in item.get("pending_calls", []):
            lines.append(
                f"    {_text(call.get('name'))} [{_text(call.get('status'))}; "
                f"no visible Result]: {_text(call.get('arguments'))}"
                + (" ... [shortened]" if call.get("arguments_truncated") else "")
            )
        if item.get("more_calls"):
            lines.append(
                "    Note: more than 20 resultless Tool declarations; "
                "use the exact transcript script for all declarations."
            )
        shortened |= bool(item.get("truncated") or item.get("arguments_truncated"))
    if entries and (shortened or data.get("summary")):
        detail = next(
            (
                item
                for item in entries
                if item.get("truncated") or item.get("arguments_truncated") or item.get("tool")
            ),
            entries[-1],
        )
        lines.append(
            "Note: compact/shortened content is not the complete transcript. Read an entry with: "
            + format_command((*base, "--entry", str(detail["seq"])))
        )
    children = data.get("subagents", [])
    if data.get("subagent_count", 0):
        lines.append(
            f"Sub-Agent Sessions (direct children): {len(children)} of {data['subagent_count']}"
        )
        for child in children:
            lines.append(
                f"  {_text(child['id'])} ({_text(child['agent_address'])}) "
                f"[{_text(child.get('activity_state'))}] "
                f"{_text(child.get('title') or child.get('auto_title'), 100)}"
            )
        if len(children) < data["subagent_count"]:
            lines.append(
                "More Sub-Agents: "
                + format_command(
                    (
                        "vbot",
                        "session",
                        "list",
                        "--parent",
                        session_id,
                        "--parent-agent",
                        owner,
                        *target_options(instance),
                    )
                )
            )
    if data.get("next_offset") is not None:
        lines.append(
            "Next: "
            + format_command(
                (*base, "--entry", str(params["entry"]), "--offset", str(data["next_offset"]))
            )
        )
    elif data.get("next_cursor") is not None:
        flags = (
            *(("--summary",) if data.get("summary") else ()),
            *(("--run", str(params["run_id"])) if params.get("run_id") else ()),
        )
        lines.append(
            "Next: "
            + format_command(
                (
                    *base,
                    *flags,
                    "--limit",
                    str(params["limit"]),
                    "--cursor",
                    encode_cursor(data["next_cursor"]),
                )
            )
        )
    return "\n".join(lines)


def _usage_line(totals: JsonObject) -> str:
    line = (
        f"input={totals.get('input_tokens', 0)} tokens; "
        f"output={totals.get('output_tokens', 0)} tokens; "
        f"cost={_usd(totals.get('cost_usd'))}"
    )
    line += (
        f" (reported {_usd(totals.get('reported_cost_usd'))}; "
        f"estimated {_usd(totals.get('estimated_cost_usd'))})"
    )
    for field, label in (
        ("unpriced_calls", "unpriced calls"),
        ("unreported_calls", "calls without reported tokens"),
        ("estimated_input_tokens", "estimated input tokens"),
        ("estimated_output_tokens", "estimated output tokens"),
    ):
        if totals.get(field):
            line += f"; {label}={totals[field]}"
    return line


def _usd(value: object) -> str:
    return f"${value:.6f}" if isinstance(value, (int, float)) else "unknown"


def _text(value: object, limit: int | None = None) -> str:
    text = inline_text(str(value)) if value is not None else "-"
    return text[:limit] + "..." if limit is not None and len(text) > limit else text


def _success(instance: ServerInstance, message: str) -> CommandResult:
    return CommandResult(ok=True, message=message, instance=instance)


def _failure(instance: ServerInstance, message: str, json_output: bool) -> CommandResult:
    return _session_list_failure(
        CommandResult(ok=False, message=message, instance=instance), json_output
    )
